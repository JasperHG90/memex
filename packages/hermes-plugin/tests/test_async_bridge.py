"""Tests for the synchronous asyncio bridge."""

from __future__ import annotations

import asyncio

import pytest

from memex_hermes_plugin.memex import async_bridge


def test_run_sync_executes_coroutine():
    async def double(x: int) -> int:
        return x * 2

    assert async_bridge.run_sync(double(21)) == 42


def test_run_sync_respects_timeout():
    async def slow() -> None:
        await asyncio.sleep(2)

    with pytest.raises(Exception):
        async_bridge.run_sync(slow(), timeout=0.05)


def test_loop_is_reused():
    async def noop() -> int:
        return 1

    async_bridge.run_sync(noop())
    loop_before = async_bridge.get_loop()
    async_bridge.run_sync(noop())
    loop_after = async_bridge.get_loop()
    assert loop_before is loop_after


def test_shutdown_is_idempotent():
    async def noop() -> int:
        return 1

    async_bridge.run_sync(noop())
    async_bridge.shutdown_loop()
    # Calling twice should not raise.
    async_bridge.shutdown_loop()
    assert not async_bridge.is_loop_running()
