"""In-process cross-encoder score cache with stampede protection.

Keys: ``(model_version, query_hash, unit_id)``. model_version makes invalidation
structural (upgrade = new prefix, no stale hits). 24h TTL backstops other paths.

Stampede protection: per-key asyncio.Lock ensures concurrent requests for the
same key collapse to a single cross-encoder call. Lock pool is a WeakValueDictionary
so locks are GC'd once no coroutine holds them. A bounded LRU pool is unsafe here —
evicting a held lock would hand a fresh lock to a second caller and break the barrier.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Awaitable, Callable, Sequence, cast
from uuid import UUID

import xxhash
from cachetools import TTLCache

from memex_core.metrics import (
    CROSS_ENCODER_CACHE_HITS_TOTAL,
    CROSS_ENCODER_CACHE_MISSES_TOTAL,
)

logger = logging.getLogger('memex.core.memory.retrieval.rerank_cache')


def hash_query(query: str) -> str:
    """Stable xxh64 hash of query text. Fast (~10x SHA-256), negligible collision rate."""
    return xxhash.xxh64(query.encode('utf-8')).hexdigest()


CacheKey = tuple[str, str, UUID]
BatchComputeFn = Callable[[Sequence[int]], Awaitable[Sequence[float]]]


class CrossEncoderScoreCache:
    """TTL cache for cross-encoder scores with per-key stampede locks."""

    def __init__(self, max_size: int = 10000, ttl_seconds: int = 86400) -> None:
        self._values: TTLCache[CacheKey, float] = TTLCache(maxsize=max_size, ttl=ttl_seconds)
        # WeakValueDictionary: locks GC'd when no coroutine holds them.
        # LRU unsafe here — evicting a held lock breaks stampede protection.
        self._locks: weakref.WeakValueDictionary[CacheKey, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._pool_lock = asyncio.Lock()

    async def _get_lock(self, key: CacheKey) -> asyncio.Lock:
        async with self._pool_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def get_if_present(self, key: CacheKey) -> float | None:
        return self._values.get(key)

    def set(self, key: CacheKey, value: float) -> None:
        self._values[key] = value

    def __len__(self) -> int:
        return len(self._values)

    @property
    def lock_pool_size(self) -> int:
        return len(self._locks)

    async def get_or_compute_batch(
        self,
        keys: Sequence[CacheKey],
        batch_compute_fn: BatchComputeFn,
    ) -> list[float]:
        """Return scores in *keys* order, computing only uncached entries.

        batch_compute_fn receives indices into *keys* for uncached entries
        and must return scores in that order. Per-key asyncio.Lock collapses
        concurrent requests for the same key to a single compute.
        """
        scores: list[float | None] = [None] * len(keys)
        miss_indices: list[int] = []

        for i, key in enumerate(keys):
            cached = self._values.get(key)
            if cached is not None:
                scores[i] = cached
                CROSS_ENCODER_CACHE_HITS_TOTAL.inc()
            else:
                miss_indices.append(i)

        if not miss_indices:
            # All keys hit — cast is preferable to list comprehension here
            # (refine type, no O(n) re-pass).
            return cast(list[float], scores)

        # Acquire per-key locks in sorted order (deadlock avoidance).
        # Dedupe: same key at multiple indices would self-deadlock.
        unique_miss_keys = sorted({keys[i] for i in miss_indices})
        locks = [await self._get_lock(k) for k in unique_miss_keys]
        for lock in locks:
            await lock.acquire()
        try:
            still_missing: list[int] = []
            for i in miss_indices:
                cached = self._values.get(keys[i])
                if cached is not None:
                    scores[i] = cached
                    CROSS_ENCODER_CACHE_HITS_TOTAL.inc()
                else:
                    still_missing.append(i)

            if still_missing:
                # Dedupe before cross-encoder dispatch: same key at multiple
                # positions would send duplicate texts. Pick one representative
                # per key, compute once, fan out the score.
                key_to_rep_index: dict[CacheKey, int] = {}
                for i in still_missing:
                    key_to_rep_index.setdefault(keys[i], i)
                rep_indices = list(key_to_rep_index.values())

                fresh = await batch_compute_fn(rep_indices)
                if len(fresh) != len(rep_indices):
                    raise RuntimeError(
                        f'batch_compute_fn returned {len(fresh)} scores, '
                        f'expected {len(rep_indices)}'
                    )

                key_to_score: dict[CacheKey, float] = dict(zip(key_to_rep_index, fresh))
                for i in still_missing:
                    score = key_to_score[keys[i]]
                    scores[i] = score
                    self._values[keys[i]] = score
                    CROSS_ENCODER_CACHE_MISSES_TOTAL.inc()
        finally:
            for lock in locks:
                lock.release()

        result: list[float] = []
        for s in scores:
            if s is None:
                raise RuntimeError('cross-encoder cache produced None score')
            result.append(s)
        return result
