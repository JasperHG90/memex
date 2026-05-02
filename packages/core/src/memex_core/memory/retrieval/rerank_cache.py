"""F41 — In-process cross-encoder score cache with stampede protection.

The cache stores raw cross-encoder logits keyed on
``(model_version, query_hash, unit_id)``.  ``model_version`` makes invalidation
structural: a model upgrade silently lookups under the new prefix and never
serves stale entries from the old one.  The 24h TTL backstops other paths.

Stampede protection
~~~~~~~~~~~~~~~~~~~
The reranker scores documents in batches.  Two concurrent retrieval calls for
the same query+unit pair MUST NOT both invoke the cross-encoder.  Per-key
``asyncio.Lock`` provides a check-and-fill barrier:

1. First pass — split the requested keys into hits and misses against the
   value cache, no locks involved.
2. For each miss, acquire that key's lock and re-check the cache (another
   coroutine may have filled it while we queued).  Truly-uncached keys fall
   through to the batch-compute step.
3. Run a single batched ``compute_fn`` on the still-uncached subset, fill the
   cache, release every lock.

The lock pool is a bounded ``LRUCache`` so it cannot grow without bound when
the working set churns.  Evicting a lock that nobody holds is safe — a fresh
lock for the same key on the next miss is functionally equivalent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Hashable, Sequence

import xxhash
from cachetools import LRUCache, TTLCache

from memex_core.metrics import (
    CROSS_ENCODER_CACHE_HITS_TOTAL,
    CROSS_ENCODER_CACHE_MISSES_TOTAL,
)

logger = logging.getLogger('memex.core.memory.retrieval.rerank_cache')


def hash_query(query: str) -> str:
    """Stable 64-bit hash of the query text — same query, same embedding, same hash.

    xxh64 is used for speed (~10× faster than SHA-256 on small strings).
    Collisions on a 1M-row working set are negligible (~6e-8) and bounded by
    the cache TTL anyway — a collision returns a wrong score for at most one
    cache entry, not a security concern.
    """
    return xxhash.xxh64(query.encode('utf-8')).hexdigest()


CacheKey = tuple[str, str, Hashable]
BatchComputeFn = Callable[[Sequence[int]], Awaitable[Sequence[float]]]


class CrossEncoderScoreCache:
    """TTL cache for cross-encoder scores with per-key stampede locks."""

    def __init__(self, max_size: int = 10000, ttl_seconds: int = 86400) -> None:
        self._values: TTLCache[CacheKey, float] = TTLCache(maxsize=max_size, ttl=ttl_seconds)
        self._locks: LRUCache[CacheKey, asyncio.Lock] = LRUCache(maxsize=max_size)
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
        """Return scores in *keys* order, computing only the missing entries.

        ``batch_compute_fn`` receives the indices (into *keys*) that were not
        served from cache and must return scores in that same order.  This
        keeps the caller in charge of the batched call signature (e.g.
        ``reranker.score(query, [texts[i] for i in idx])``).

        Each missing key's compute is barriered behind a per-key
        ``asyncio.Lock`` so concurrent requests for the same key collapse
        to a single cross-encoder call.
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
            return [s for s in scores if s is not None]  # type: ignore[misc]

        # Acquire per-key locks in a globally-consistent order to avoid
        # deadlock when two concurrent calls request the same keys in
        # different orders. Dedupe first — a single key may appear at
        # multiple miss_indices (same unit referenced twice in a query)
        # and re-acquiring its lock would deadlock the same coroutine.
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
                fresh = await batch_compute_fn(still_missing)
                if len(fresh) != len(still_missing):
                    raise RuntimeError(
                        f'batch_compute_fn returned {len(fresh)} scores, '
                        f'expected {len(still_missing)}'
                    )
                for i, score in zip(still_missing, fresh):
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
