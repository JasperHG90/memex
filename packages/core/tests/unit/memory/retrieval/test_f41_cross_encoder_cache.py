"""F41 — cross-encoder score cache unit tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Sequence
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from memex_common.config import RetrievalConfig
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.rerank_cache import (
    CrossEncoderScoreCache,
    hash_query,
)
from memex_core.memory.sql_models import MemoryUnit
from memex_core.metrics import (
    CROSS_ENCODER_CACHE_HITS_TOTAL,
    CROSS_ENCODER_CACHE_MISSES_TOTAL,
)


def _read_metric(metric: object) -> float:
    samples = list(metric.collect()[0].samples)  # type: ignore[attr-defined]
    for s in samples:
        if s.name.endswith('_total'):
            return float(s.value)
    return 0.0


def _make_unit(unit_id: UUID | None = None, text: str = 'fact') -> MemoryUnit:
    return MemoryUnit(
        id=unit_id or uuid4(),
        text=text,
        fact_type='fact',
        event_date=datetime.now(timezone.utc),
        vault_id=uuid4(),
        note_id=uuid4(),
        embedding=[],
        success_co_count=0,
        failure_co_count=0,
    )


def _make_engine(
    scores: list[float] | None = None,
    cache_enabled: bool = True,
    cache_size: int = 100,
    ttl: int = 86400,
    reranker: MagicMock | None = None,
) -> RetrievalEngine:
    if reranker is None:
        reranker = MagicMock()
        reranker.score.return_value = scores or [0.0]
        reranker.model_version = 'onnx:test:v1'
    config = RetrievalConfig(
        cross_encoder_cache_enabled=cache_enabled,
        cross_encoder_cache_size=cache_size,
        cross_encoder_cache_ttl_seconds=ttl,
    )
    return RetrievalEngine(
        embedder=MagicMock(),
        reranker=reranker,
        retrieval_config=config,
    )


class TestHashQuery:
    def test_stable_for_same_input(self) -> None:
        assert hash_query('what is the meaning?') == hash_query('what is the meaning?')

    def test_differs_for_different_input(self) -> None:
        assert hash_query('a') != hash_query('b')


class TestCacheUnit:
    @pytest.mark.asyncio
    async def test_cold_cache_calls_compute_and_increments_misses(self) -> None:
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=60)
        before = _read_metric(CROSS_ENCODER_CACHE_MISSES_TOTAL)

        calls: list[Sequence[int]] = []

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            calls.append(list(missing))
            return [1.0 for _ in missing]

        keys = [('m', 'q', uuid4()) for _ in range(3)]
        out = await cache.get_or_compute_batch(keys, compute)

        assert out == [1.0, 1.0, 1.0]
        assert calls == [[0, 1, 2]]
        assert _read_metric(CROSS_ENCODER_CACHE_MISSES_TOTAL) == before + 3

    @pytest.mark.asyncio
    async def test_warm_cache_skips_compute_and_increments_hits(self) -> None:
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=60)
        keys = [('m', 'q', uuid4()) for _ in range(2)]

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            return [0.5 for _ in missing]

        await cache.get_or_compute_batch(keys, compute)

        before_hits = _read_metric(CROSS_ENCODER_CACHE_HITS_TOTAL)

        async def boom(missing: Sequence[int]) -> Sequence[float]:
            raise AssertionError('compute should not be called on warm cache')

        out = await cache.get_or_compute_batch(keys, boom)
        assert out == [0.5, 0.5]
        assert _read_metric(CROSS_ENCODER_CACHE_HITS_TOTAL) == before_hits + 2

    @pytest.mark.asyncio
    async def test_partial_hits_compute_only_misses(self) -> None:
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=60)
        u1, u2, u3 = uuid4(), uuid4(), uuid4()
        cache.set(('m', 'q', u1), 0.1)
        cache.set(('m', 'q', u3), 0.3)

        captured: list[Sequence[int]] = []

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            captured.append(list(missing))
            return [0.2]

        out = await cache.get_or_compute_batch(
            [('m', 'q', u1), ('m', 'q', u2), ('m', 'q', u3)], compute
        )
        assert out == [0.1, 0.2, 0.3]
        assert captured == [[1]]

    @pytest.mark.asyncio
    async def test_concurrent_stampede_collapses_to_single_compute(self) -> None:
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=60)
        key = ('m', 'q', uuid4())

        compute_count = 0
        gate = asyncio.Event()

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            nonlocal compute_count
            compute_count += 1
            await gate.wait()
            return [42.0 for _ in missing]

        async def call() -> float:
            scores = await cache.get_or_compute_batch([key], compute)
            return scores[0]

        coros = [call() for _ in range(10)]
        task = asyncio.gather(*coros)
        await asyncio.sleep(0.05)
        gate.set()
        results = await task

        assert all(r == 42.0 for r in results)
        assert compute_count == 1

    @pytest.mark.asyncio
    async def test_different_keys_compute_independently(self) -> None:
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=60)

        async def compute_a(missing: Sequence[int]) -> Sequence[float]:
            return [1.0 for _ in missing]

        async def compute_b(missing: Sequence[int]) -> Sequence[float]:
            return [2.0 for _ in missing]

        keys_a = [('m', 'q', uuid4())]
        keys_b = [('m', 'q', uuid4())]
        out_a = await cache.get_or_compute_batch(keys_a, compute_a)
        out_b = await cache.get_or_compute_batch(keys_b, compute_b)
        assert out_a == [1.0]
        assert out_b == [2.0]

    @pytest.mark.asyncio
    async def test_ttl_expiry_recomputes(self) -> None:
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=1)
        key = ('m', 'q', uuid4())

        n = 0

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            nonlocal n
            n += 1
            return [float(n)]

        first = await cache.get_or_compute_batch([key], compute)
        cache._values.clear()
        second = await cache.get_or_compute_batch([key], compute)
        assert first == [1.0]
        assert second == [2.0]

    @pytest.mark.asyncio
    async def test_overlapping_keys_in_different_orders_do_not_deadlock(self) -> None:
        """Hermes round-1 CRITICAL — without globally-consistent lock ordering,
        two callers acquiring the same per-key locks in different orders would
        deadlock. Both calls must finish under timeout."""
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=60)
        u1, u2 = uuid4(), uuid4()
        key_a = ('m', 'q', u1)
        key_b = ('m', 'q', u2)

        gate = asyncio.Event()
        compute_started = asyncio.Event()
        active = 0

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            nonlocal active
            active += 1
            compute_started.set()
            # Hold both compute calls inside the locked region simultaneously
            # so a deadlock (if it existed) would be observable.
            await gate.wait()
            return [1.0 for _ in missing]

        async def call(keys: list[tuple[str, str, UUID]]) -> list[float]:
            return await cache.get_or_compute_batch(keys, compute)

        # Coro 1 will queue locks in [key_a, key_b] order (without sorting).
        # Coro 2 will queue them in [key_b, key_a] order — the classic AB/BA
        # deadlock pattern.
        task = asyncio.gather(
            call([key_a, key_b]),
            call([key_b, key_a]),
        )
        # Drive scheduler so both coroutines have a chance to interleave on
        # the lock acquisitions before we release the gate.
        await asyncio.sleep(0.05)
        gate.set()
        results = await asyncio.wait_for(task, timeout=2.0)

        assert results[0] == [1.0, 1.0]
        assert results[1] == [1.0, 1.0]

    @pytest.mark.asyncio
    async def test_repeated_key_in_single_call_does_not_self_deadlock(self) -> None:
        """A query may legitimately reference the same unit twice (RRF dedupe
        runs *after* this layer). Acquiring the same lock twice in one
        coroutine would deadlock — the dedupe must collapse it."""
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=60)
        u1 = uuid4()
        key = ('m', 'q', u1)

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            return [7.0 for _ in missing]

        out = await asyncio.wait_for(
            cache.get_or_compute_batch([key, key, key], compute), timeout=2.0
        )
        assert out == [7.0, 7.0, 7.0]

    @pytest.mark.asyncio
    async def test_lru_lock_pool_evicts_at_cap(self) -> None:
        cache = CrossEncoderScoreCache(max_size=2, ttl_seconds=60)

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            return [0.0 for _ in missing]

        for _ in range(5):
            await cache.get_or_compute_batch([('m', 'q', uuid4())], compute)

        assert cache.lock_pool_size <= 2

    @pytest.mark.asyncio
    async def test_compute_fn_size_mismatch_raises(self) -> None:
        cache = CrossEncoderScoreCache(max_size=10, ttl_seconds=60)

        async def compute(missing: Sequence[int]) -> Sequence[float]:
            return [0.0]

        with pytest.raises(RuntimeError, match='returned 1 scores'):
            await cache.get_or_compute_batch([('m', 'q', uuid4()), ('m', 'q', uuid4())], compute)


class TestEngineIntegration:
    @pytest.mark.asyncio
    async def test_default_on_with_cache_avoids_second_call(self) -> None:
        unit = _make_unit()
        engine = _make_engine(scores=[1.0])

        await engine._rerank_results('q', [unit])
        await engine._rerank_results('q', [unit])

        assert engine.reranker.score.call_count == 1  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_cache_disabled_calls_reranker_every_time(self) -> None:
        unit = _make_unit()
        engine = _make_engine(scores=[1.0], cache_enabled=False)

        await engine._rerank_results('q', [unit])
        await engine._rerank_results('q', [unit])

        assert engine.reranker.score.call_count == 2  # type: ignore[union-attr]
        assert engine._rerank_cache is None

    @pytest.mark.asyncio
    async def test_different_unit_id_misses_cache(self) -> None:
        u1 = _make_unit()
        u2 = _make_unit()
        reranker = MagicMock()
        reranker.score.side_effect = [[1.0], [2.0]]
        reranker.model_version = 'onnx:test:v1'
        engine = _make_engine(reranker=reranker)

        await engine._rerank_results('q', [u1])
        await engine._rerank_results('q', [u2])

        assert reranker.score.call_count == 2

    @pytest.mark.asyncio
    async def test_different_query_misses_cache(self) -> None:
        u1 = _make_unit()
        reranker = MagicMock()
        reranker.score.side_effect = [[1.0], [2.0]]
        reranker.model_version = 'onnx:test:v1'
        engine = _make_engine(reranker=reranker)

        await engine._rerank_results('q1', [u1])
        await engine._rerank_results('q2', [u1])

        assert reranker.score.call_count == 2

    @pytest.mark.asyncio
    async def test_model_version_change_invalidates_structurally(self) -> None:
        u1 = _make_unit()
        reranker = MagicMock()
        reranker.score.side_effect = [[1.0], [2.0]]
        # Use a property that flips between calls
        versions = iter(['onnx:test:v1', 'onnx:test:v2'])
        type(reranker).model_version = property(lambda _self: next(versions))
        engine = _make_engine(reranker=reranker)

        await engine._rerank_results('q', [u1])
        await engine._rerank_results('q', [u1])

        assert reranker.score.call_count == 2

    @pytest.mark.asyncio
    async def test_partial_batch_only_computes_misses(self) -> None:
        u1 = _make_unit()
        u2 = _make_unit()
        reranker = MagicMock()
        # First call: both u1, u2 -> [1.0, 2.0]
        # Second call: only u3 should be computed since u1 cached
        reranker.score.side_effect = [[1.0, 2.0], [3.0]]
        reranker.model_version = 'onnx:test:v1'
        engine = _make_engine(reranker=reranker)

        await engine._rerank_results('q', [u1, u2])

        u3 = _make_unit()
        await engine._rerank_results('q', [u1, u3])

        assert reranker.score.call_count == 2
        # Second call should only have asked for u3's text
        second_call_args = reranker.score.call_args_list[1]
        _, second_texts = second_call_args.args
        assert len(second_texts) == 1
