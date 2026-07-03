"""Synchronous bridge to ``RemoteMemexAPI`` (which is async-only).

Hermes calls memory provider methods synchronously, so we run a single
long-lived asyncio loop on a daemon thread and marshal coroutines onto it
with ``asyncio.run_coroutine_threadsafe``. One loop per process, reused.

Adapted from the Hindsight plugin's approach. Using ``asyncio.run()`` per
call is ~10x slower and leaks aiohttp sessions.

Lifetime invariant
==================
The shared loop is **process-lifetime** — it is created on first use and
torn down only when the daemon thread dies at process exit. Production
code MUST NOT call ``shutdown_loop()``: closing the loop while other
threads are mid-``run_sync`` opens a race where
``run_coroutine_threadsafe`` invokes ``call_soon_threadsafe`` on a
closed loop, raising ``RuntimeError('Event loop is closed')`` instantly
from ``base_events._check_closed``. The error is sub-millisecond
(pre-IO) and indistinguishable from a transport-layer failure.

``shutdown_loop()`` exists only for ``_reset_for_tests``. ``run_sync``
defensively retries once on ``RuntimeError`` so that a test-only loop
swap mid-call does not flake.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, TypeVar

T = TypeVar('T')

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide event loop, starting it on first call."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and not _loop.is_closed() and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        started = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(_loop)
            assert _loop is not None
            _loop.call_soon(started.set)
            _loop.run_forever()

        _loop_thread = threading.Thread(target=_run, daemon=True, name='memex-hermes-loop')
        _loop_thread.start()
        loop_ref = _loop

    # Block until run_forever has actually started, so callers never receive
    # a loop reference whose call_soon_threadsafe could race with startup.
    started.wait(timeout=5.0)
    return loop_ref


def run_sync(coro: Awaitable[T], timeout: float = 120.0) -> T:
    """Schedule ``coro`` on the shared loop and block until it completes.

    Defends against the loop-close race: if ``call_soon_threadsafe`` finds
    the loop closed (a test ``_reset_for_tests`` fired between ``get_loop``
    returning and us submitting), retry once on a fresh loop. In production
    the loop is process-lifetime and this branch is unreachable.
    """
    try:
        future = asyncio.run_coroutine_threadsafe(coro, get_loop())  # type: ignore[arg-type]
    except RuntimeError as e:
        if 'Event loop is closed' not in str(e):
            raise
        global _loop, _loop_thread
        with _loop_lock:
            _loop = None
            _loop_thread = None
        future = asyncio.run_coroutine_threadsafe(coro, get_loop())  # type: ignore[arg-type]
    return future.result(timeout=timeout)


def shutdown_loop(thread_join_timeout: float = 5.0) -> None:
    """Stop the shared loop, join its thread, and close the loop. Idempotent.

    WARNING: production code MUST NOT call this. See the module docstring
    for the lifetime invariant. Reserved for ``_reset_for_tests`` and
    teardown utilities that own the process lifecycle.
    """
    global _loop, _loop_thread
    with _loop_lock:
        loop = _loop
        thread = _loop_thread
        _loop = None
        _loop_thread = None

    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread.is_alive():
        thread.join(timeout=thread_join_timeout)
    if loop is not None and not loop.is_closed():
        try:
            loop.close()
        except Exception:
            pass


def is_loop_running() -> bool:
    """Return True iff the shared loop is active. For tests / diagnostics."""
    with _loop_lock:
        return _loop is not None and _loop.is_running()


def _reset_for_tests() -> None:
    """Test helper — forcibly clear module-level state without a clean stop.

    Never call from production code.
    """
    global _loop, _loop_thread
    shutdown_loop()
    with _loop_lock:
        _loop = None
        _loop_thread = None


__all__ = ['get_loop', 'run_sync', 'shutdown_loop', 'is_loop_running']
