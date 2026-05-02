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

The lock pool is a ``WeakValueDictionary`` so it does not pin locks beyond
the lifetime of the coroutines that hold them.  Concurrent callers each
keep a strong reference for the duration of the locked region; once they
all release, the entry is collected.  A bounded LRU lock pool is unsafe
here — evicting a still-held lock would hand a fresh lock to a second
caller and break stampede protection.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Awaitable, Callable, Sequence
from uuid import UUID

import xxhash
from cachetools import TTLCache

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


CacheKey = tuple[str, str, UUID]
BatchComputeFn = Callable[[Sequence[int]], Awaitable[Sequence[float]]]


class CrossEncoderScoreCache:
    """TTL cache for cross-encoder scores with per-key stampede locks."""

    def __init__(self, max_size: int = 10000, ttl_seconds: int = 86400) -> None:
        self._values: TTLCache[CacheKey, float] = TTLCache(maxsize=max_size, ttl=ttl_seconds)
        # Hermes round-1 MED — lock pool uses ``WeakValueDictionary`` so a
        # lock entry is only collected once nothing else holds it. An
        # LRUCache here could evict a lock while a coroutine still owned
        # it, breaking stampede protection (a fresh lock for the same key
        # would let a second coroutine bypass the barrier and duplicate
        # work). Callers MUST keep a strong reference for the duration of
        # the locked region — ``get_or_compute_batch`` does this by
        # accumulating locks into a local list before acquiring.
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
                # Hermes round-2 MED — dedupe before dispatching to the
                # cross-encoder. The same key may appear at multiple positions
                # in *keys* (RRF dedupe runs after this layer); locks already
                # collapse to one acquisition per unique key (above), but the
                # compute path would still send duplicate texts to the model
                # and pay the GPU cost N times. Pick one representative index
                # per unique key, compute once, fan the score back to every
                # position sharing that key.
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
