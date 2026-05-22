"""Regression tests for the 'Event loop is closed' bug in the async bridge.

ROOT CAUSE
==========
``run_sync`` is::

    def run_sync(coro, timeout=120.0):
        future = asyncio.run_coroutine_threadsafe(coro, get_loop())
        return future.result(timeout=timeout)

There is a race window between ``get_loop()`` returning a live loop and
``run_coroutine_threadsafe`` invoking ``loop.call_soon_threadsafe`` on it.
If anything closes the loop in that window — historically
``provider.shutdown()`` did, via ``shutdown_loop()`` — ``call_soon_threadsafe``
calls ``base_events._check_closed`` and raises ``RuntimeError('Event loop
is closed')`` immediately, before any IO. That matches the production
observation: "0.02s after call, pre-IO, asyncio/transport layer."

FIX
===
Two layers:

1. ``provider.shutdown()`` no longer calls ``shutdown_loop()``. The bridge
   loop is process-lifetime; the daemon thread is reaped at process exit.
2. ``run_sync`` defensively retries once on ``RuntimeError('Event loop is
   closed')``, so a test ``_reset_for_tests`` mid-call cannot flake.

These tests pin both layers down.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from memex_hermes_plugin.memex import async_bridge


# --------------------------------------------------------------------------- #
# Mechanism — proves the bug is real if the fix is reverted                   #
# --------------------------------------------------------------------------- #


def test_call_soon_threadsafe_on_closed_loop_raises_loop_closed() -> None:
    """Pins the asyncio-layer raise: closed loop + run_coroutine_threadsafe.

    This is the exact mechanism that surfaced as the production failure.
    The test does NOT exercise the bridge; it pins the asyncio contract
    so anyone reverting the fix in the bridge re-introduces this error.
    """

    async def noop() -> int:
        return 1

    async_bridge.run_sync(noop())
    loop = async_bridge.get_loop()
    async_bridge.shutdown_loop()
    assert loop.is_closed()

    coro = noop()
    try:
        with pytest.raises(RuntimeError, match='Event loop is closed'):
            asyncio.run_coroutine_threadsafe(coro, loop)
    finally:
        coro.close()


# --------------------------------------------------------------------------- #
# Layer 1: provider.shutdown() must NOT tear down the bridge loop             #
# --------------------------------------------------------------------------- #


def test_provider_shutdown_does_not_call_shutdown_loop() -> None:
    """provider.shutdown() must leave the bridge loop running.

    Killing the loop mid-process opens the race window other run_sync
    callers (prefetch, briefing) live inside. The bridge loop is
    process-lifetime — daemon thread reaped at process exit.
    """
    from memex_hermes_plugin.memex.provider import MemexMemoryProvider

    provider = MemexMemoryProvider()
    async_bridge.run_sync(asyncio.sleep(0))
    assert async_bridge.is_loop_running()

    provider.shutdown()

    assert async_bridge.is_loop_running(), (
        'provider.shutdown() closed the bridge loop. This re-opens the '
        '"Event loop is closed" race. See async_bridge module docstring.'
    )


def test_provider_module_does_not_import_shutdown_loop() -> None:
    """Boundary fence: the provider must not have shutdown_loop in scope.

    Importing it makes it cheap to re-introduce the call. Drop the import
    when you drop the call.
    """
    from memex_hermes_plugin.memex import provider as provider_module

    assert not hasattr(provider_module, 'shutdown_loop'), (
        'provider imports shutdown_loop. The bridge loop is '
        'process-lifetime; provider has no business closing it. '
        'See async_bridge module docstring for the lifetime invariant.'
    )


# --------------------------------------------------------------------------- #
# Layer 2: run_sync defends against test-only loop swaps mid-call             #
# --------------------------------------------------------------------------- #


def test_run_sync_recovers_from_loop_swap_between_get_and_submit() -> None:
    """Simulate the race: get_loop returns, then loop closes, then submit.

    Before the defensive retry in run_sync, this raised ``RuntimeError:
    Event loop is closed``. After the fix, run_sync notices the closed
    loop and retries once against a fresh one.
    """
    original_get_loop = async_bridge.get_loop
    call_count = [0]

    def get_loop_then_close():
        loop = original_get_loop()
        call_count[0] += 1
        if call_count[0] == 1:
            # Mimic the race: another thread shuts down between
            # get_loop() returning and run_coroutine_threadsafe being
            # called. We do it inline so the test is deterministic.
            async_bridge.shutdown_loop()
        return loop

    async def trivial() -> int:
        return 42

    with patch.object(async_bridge, 'get_loop', side_effect=get_loop_then_close):
        result = async_bridge.run_sync(trivial(), timeout=3.0)

    assert result == 42
    assert call_count[0] == 2, f'Expected exactly one retry (2 get_loop calls), got {call_count[0]}'


def test_run_sync_does_not_retry_on_unrelated_runtime_error() -> None:
    """The defensive retry must only fire for 'Event loop is closed'.

    Other RuntimeErrors propagate as-is so real bugs are not swallowed.
    """

    async def boom() -> None:
        raise RuntimeError('something else')

    with pytest.raises(RuntimeError, match='something else'):
        async_bridge.run_sync(boom(), timeout=3.0)


# --------------------------------------------------------------------------- #
# Probabilistic confirmation the race exists if you turn defenses off         #
# --------------------------------------------------------------------------- #


def test_concurrent_shutdown_hammer_without_retry_would_race() -> None:
    """Documents the historic race: bridge-only, no provider involvement.

    Hammer run_sync from N threads while toggling shutdown_loop. If the
    defensive retry in run_sync is removed (or shutdown_loop is called
    from production code), this test will see 'Event loop is closed'.

    With the current fix, run_sync absorbs the swap and the test always
    succeeds. Skipped if no race triggered within budget — the
    deterministic test above is authoritative; this one is here to give
    a humanly believable demo on a real machine.
    """

    async def trivial() -> int:
        return 1

    async_bridge.run_sync(trivial())

    captured_unrecovered: list[BaseException] = []
    stop = threading.Event()

    def hammerer() -> None:
        while not stop.is_set():
            try:
                async_bridge.run_sync(trivial(), timeout=2.0)
            except RuntimeError as e:
                if 'Event loop is closed' in str(e):
                    captured_unrecovered.append(e)
                    return
            except Exception:
                pass

    workers = [threading.Thread(target=hammerer, daemon=True) for _ in range(8)]
    for w in workers:
        w.start()

    for _ in range(40):
        time.sleep(0.005)
        async_bridge.shutdown_loop()
        time.sleep(0.001)
        async_bridge.get_loop()

    stop.set()
    for w in workers:
        w.join(timeout=2.0)

    assert not captured_unrecovered, (
        f'run_sync surfaced unrecovered loop-closed errors despite the '
        f'defensive retry: {captured_unrecovered}'
    )
