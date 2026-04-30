"""In-process token-bucket rate limiter (F5).

Per-key token bucket with monotonic-clock refill. Designed for advisory
limits, not security gates: multi-worker leakage is accepted in v1
(F9 introduces distributed locking; F38 will reuse this primitive).

Reusable across F5, F9, F10, F20 per RFC-002.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


class RateLimitExceededError(Exception):
    """Raised when a rate-limited operation is attempted before the bucket has refilled.

    ``retry_after_seconds`` is the suggested back-off; service callers surface it
    structurally to clients (MCP tools, Hermes handlers).
    """

    def __init__(self, key: object, retry_after_seconds: float, *, message: str | None = None):
        self.key = key
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            message or f'Rate limit exceeded for {key!r}; retry after {retry_after_seconds:.2f}s.'
        )


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    """LRU token-bucket keyed by an arbitrary hashable.

    Each key gets a bucket of capacity ``burst`` that refills at
    ``burst / per_seconds`` tokens per second. ``acquire(key)`` consumes
    one token or raises ``RateLimitExceededError``.

    Thread-safe via a single mutex (in-process; multi-worker leakage is
    documented in RFC-002 and acknowledged in PR body per AC-F5-4).
    """

    def __init__(
        self,
        *,
        per_seconds: float,
        burst: int = 1,
        max_keys: int = 10000,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_seconds <= 0:
            raise ValueError('per_seconds must be > 0')
        if burst <= 0:
            raise ValueError('burst must be > 0')
        if max_keys <= 0:
            raise ValueError('max_keys must be > 0')
        self._per_seconds = float(per_seconds)
        self._burst = float(burst)
        self._refill_rate = self._burst / self._per_seconds
        self._max_keys = max_keys
        self._enabled = enabled
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: OrderedDict[object, _Bucket] = OrderedDict()

    def acquire(self, key: object) -> None:
        """Consume one token for ``key`` or raise ``RateLimitExceededError``."""
        if not self._enabled:
            return
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._burst, last_refill=now)
                self._buckets[key] = bucket
                self._buckets.move_to_end(key)
                self._evict_if_needed()
            else:
                self._buckets.move_to_end(key)
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._refill_rate)
                bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return

            deficit = 1.0 - bucket.tokens
            retry_after = deficit / self._refill_rate
            raise RateLimitExceededError(key, retry_after_seconds=retry_after)

    def _evict_if_needed(self) -> None:
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
