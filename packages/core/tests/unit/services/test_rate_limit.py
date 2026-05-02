"""Unit tests for TokenBucketRateLimiter (F5 TC1).

Frozen-clock pattern per TS guidance: a list-based clock advances only when
the test code chooses, so refill timing is deterministic regardless of
wall-clock variance.
"""

from __future__ import annotations

import pytest

from memex_core.services.rate_limit import RateLimitExceededError, TokenBucketRateLimiter


class _FrozenClock:
    """Monotonic clock stub. ``advance(seconds)`` moves it forward."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.mark.asyncio
async def test_bucket_drain_and_refill():
    """Bucket allows `burst` calls then blocks; refills after per_seconds elapses."""
    clock = _FrozenClock()
    limiter = TokenBucketRateLimiter(per_seconds=60.0, burst=1, clock=clock)

    await limiter.acquire('entity-A')

    with pytest.raises(RateLimitExceededError) as exc:
        await limiter.acquire('entity-A')
    assert 0 < exc.value.retry_after_seconds <= 60.0
    assert exc.value.key == 'entity-A'

    clock.advance(60.0)
    await limiter.acquire('entity-A')


@pytest.mark.asyncio
async def test_per_key_isolation():
    """A blocked key does not block other keys."""
    clock = _FrozenClock()
    limiter = TokenBucketRateLimiter(per_seconds=60.0, burst=1, clock=clock)

    await limiter.acquire(('entity-A', 'vault-X'))
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire(('entity-A', 'vault-X'))

    await limiter.acquire(('entity-B', 'vault-X'))
    await limiter.acquire(('entity-A', 'vault-Y'))


@pytest.mark.asyncio
async def test_enabled_false_is_passthrough():
    """When enabled=False, acquire never raises."""
    clock = _FrozenClock()
    limiter = TokenBucketRateLimiter(per_seconds=60.0, burst=1, enabled=False, clock=clock)
    for _ in range(100):
        await limiter.acquire('entity-A')


@pytest.mark.asyncio
async def test_burst_capacity_allows_back_to_back_calls_within_window():
    """burst=N permits N back-to-back calls before blocking."""
    clock = _FrozenClock()
    limiter = TokenBucketRateLimiter(per_seconds=60.0, burst=3, clock=clock)
    await limiter.acquire('A')
    await limiter.acquire('A')
    await limiter.acquire('A')
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire('A')


@pytest.mark.asyncio
async def test_lru_eviction_caps_tracked_keys():
    """Once max_keys is exceeded, oldest key is evicted; eviction does not raise."""
    clock = _FrozenClock()
    limiter = TokenBucketRateLimiter(per_seconds=60.0, burst=1, max_keys=2, clock=clock)
    await limiter.acquire('A')
    await limiter.acquire('B')
    await limiter.acquire('C')
    await limiter.acquire('A')


def test_invalid_config_raises():
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(per_seconds=0)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(per_seconds=60, burst=0)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(per_seconds=60, max_keys=0)
