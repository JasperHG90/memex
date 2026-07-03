"""Unit tests for the per-entity asyncio.Lock helper."""

from __future__ import annotations

import asyncio
import gc
from uuid import uuid4

import pytest

from memex_core.memory.reflect.entity_locks import (
    _registry_size_for_tests,
    get_entity_lock,
)


class TestGetEntityLockIdentity:
    @pytest.mark.asyncio
    async def test_same_entity_id_returns_same_lock(self) -> None:
        eid = uuid4()
        lock_a = await get_entity_lock(eid)
        lock_b = await get_entity_lock(eid)
        assert lock_a is lock_b

    @pytest.mark.asyncio
    async def test_different_entity_ids_return_distinct_locks(self) -> None:
        lock_a = await get_entity_lock(uuid4())
        lock_b = await get_entity_lock(uuid4())
        assert lock_a is not lock_b


class TestSerializationOnSameEntity:
    @pytest.mark.asyncio
    async def test_two_coroutines_on_same_entity_serialize(self) -> None:
        eid = uuid4()
        order: list[str] = []

        async def worker(label: str, hold_for: float) -> None:
            lock = await get_entity_lock(eid)
            async with lock:
                order.append(f'{label}:enter')
                await asyncio.sleep(hold_for)
                order.append(f'{label}:exit')

        await asyncio.gather(worker('A', 0.05), worker('B', 0.05))

        # Sequencing guarantees one full A pair or one full B pair, never interleaved
        assert order in (
            ['A:enter', 'A:exit', 'B:enter', 'B:exit'],
            ['B:enter', 'B:exit', 'A:enter', 'A:exit'],
        )


class TestParallelismOnDifferentEntities:
    @pytest.mark.asyncio
    async def test_two_coroutines_on_distinct_entities_run_in_parallel(self) -> None:
        eid_a = uuid4()
        eid_b = uuid4()
        hold = 0.1

        async def worker(eid):
            lock = await get_entity_lock(eid)
            async with lock:
                await asyncio.sleep(hold)

        loop = asyncio.get_event_loop()
        start = loop.time()
        await asyncio.gather(worker(eid_a), worker(eid_b))
        elapsed = loop.time() - start
        # Wall-clock should be ~hold, not ~2*hold — adding a margin for jitter
        assert elapsed < hold * 1.6, (
            f'parallel entities should not serialize; elapsed={elapsed:.3f} expected~={hold:.3f}'
        )


class TestWeakEviction:
    @pytest.mark.asyncio
    async def test_lock_evicted_when_no_references_held(self) -> None:
        baseline = _registry_size_for_tests()
        eid = uuid4()
        lock = await get_entity_lock(eid)
        assert _registry_size_for_tests() == baseline + 1
        del lock
        gc.collect()
        # WeakValueDictionary should have dropped the entry now
        assert _registry_size_for_tests() == baseline

    @pytest.mark.asyncio
    async def test_lock_retained_while_caller_holds_reference(self) -> None:
        baseline = _registry_size_for_tests()
        eid = uuid4()
        lock = await get_entity_lock(eid)
        gc.collect()
        # Live reference => not evicted
        assert _registry_size_for_tests() == baseline + 1
        # And get_entity_lock still returns the same instance
        again = await get_entity_lock(eid)
        assert again is lock


class TestConcurrentLazyInit:
    @pytest.mark.asyncio
    async def test_concurrent_first_acquisitions_get_same_lock(self) -> None:
        eid = uuid4()

        async def acquire():
            return await get_entity_lock(eid)

        locks = await asyncio.gather(*[acquire() for _ in range(8)])
        first = locks[0]
        for lock in locks[1:]:
            assert lock is first
