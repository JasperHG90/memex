"""Task #33 — F9 consolidate-vault rate-limit (RFC-008 line 125).

Per-vault TokenBucket, default 1 call/hour. Reuses F5's
``TokenBucketRateLimiter`` primitive. These tests verify the
service-layer wiring and per-vault isolation; the bucket primitive
itself is exercised in ``test_rate_limit.py``.

Tests:
- ``test_consolidate_rate_limit_first_call_succeeds_second_blocks``: with
  burst=1, the second call within the window raises
  ``RateLimitExceededError`` carrying ``retry_after_seconds``.
- ``test_consolidate_rate_limit_sliding_1h_window``: after the full
  ``per_vault_per_seconds`` window elapses, the bucket has refilled and
  the next call succeeds. Frozen-clock keeps timing deterministic.
- ``test_consolidate_rate_limit_isolated_per_vault``: a blocked vault
  does not block other vaults — keys are vault-scoped.
- ``test_consolidate_rate_limit_dry_run_is_not_charged``: ``dry_run=True``
  bypasses the limiter so agents can preview cheaply.
- ``test_consolidate_rate_limit_disabled_via_config``: when
  ``enabled=False`` the limiter is a passthrough.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.services.locks import LocksService
from memex_core.services.rate_limit import RateLimitExceededError, TokenBucketRateLimiter
from memex_core.services.reflection import ReflectionService
from memex_core.services.units import UnitsService


class _FrozenClock:
    """Monotonic clock stub. ``advance(seconds)`` moves it forward."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _make_locks_service(*, enabled: bool, clock: _FrozenClock) -> LocksService:
    """Build a LocksService whose limiter uses a frozen clock.

    Construction bypasses ``__init__`` (which derives a DSN from a real
    config) — we only exercise ``consolidate_vault`` and stub out the
    candidate selector + units dependency, so DSN never gets used.
    """
    contradiction = MagicMock(spec=ContradictionEngine)
    reflection = MagicMock(spec=ReflectionService)
    units = MagicMock(spec=UnitsService)
    units.set_unit_deprioritized = AsyncMock(return_value=None)

    svc = LocksService.__new__(LocksService)
    svc.metastore = MagicMock()
    svc.config = MagicMock()
    svc.reflection = reflection
    svc.contradiction = contradiction
    svc.units = units
    svc._dsn = 'postgresql://stub'  # never opened in these tests
    svc._pool = None
    svc._has_maintenance_proposals_table_cache = False
    svc._consolidate_limiter = TokenBucketRateLimiter(
        per_seconds=3600.0,
        burst=1,
        max_keys=1000,
        enabled=enabled,
        clock=clock,
    )

    async def _no_candidates(_vault_id: uuid.UUID) -> list[uuid.UUID]:
        return []

    svc._select_consolidate_candidates = _no_candidates  # type: ignore[method-assign]

    return svc


@pytest.mark.asyncio
async def test_consolidate_rate_limit_first_call_succeeds_second_blocks() -> None:
    clock = _FrozenClock()
    svc = _make_locks_service(enabled=True, clock=clock)
    vault_id = GLOBAL_VAULT_ID

    result = await svc.consolidate_vault(vault_id, dry_run=False)
    assert result['vault_id'] == str(vault_id)
    assert result['dry_run'] is False

    with pytest.raises(RateLimitExceededError) as exc:
        await svc.consolidate_vault(vault_id, dry_run=False)
    assert exc.value.key == vault_id
    assert 0 < exc.value.retry_after_seconds <= 3600.0


@pytest.mark.asyncio
async def test_consolidate_rate_limit_sliding_1h_window() -> None:
    clock = _FrozenClock()
    svc = _make_locks_service(enabled=True, clock=clock)
    vault_id = GLOBAL_VAULT_ID

    await svc.consolidate_vault(vault_id, dry_run=False)
    with pytest.raises(RateLimitExceededError):
        await svc.consolidate_vault(vault_id, dry_run=False)

    clock.advance(3599.9)
    with pytest.raises(RateLimitExceededError):
        await svc.consolidate_vault(vault_id, dry_run=False)

    clock.advance(1.0)
    await svc.consolidate_vault(vault_id, dry_run=False)


@pytest.mark.asyncio
async def test_consolidate_rate_limit_isolated_per_vault() -> None:
    clock = _FrozenClock()
    svc = _make_locks_service(enabled=True, clock=clock)
    vault_a = uuid.uuid4()
    vault_b = uuid.uuid4()

    await svc.consolidate_vault(vault_a, dry_run=False)
    with pytest.raises(RateLimitExceededError):
        await svc.consolidate_vault(vault_a, dry_run=False)

    await svc.consolidate_vault(vault_b, dry_run=False)
    with pytest.raises(RateLimitExceededError):
        await svc.consolidate_vault(vault_b, dry_run=False)


@pytest.mark.asyncio
async def test_consolidate_rate_limit_dry_run_is_not_charged() -> None:
    clock = _FrozenClock()
    svc = _make_locks_service(enabled=True, clock=clock)
    vault_id = GLOBAL_VAULT_ID

    for _ in range(5):
        result = await svc.consolidate_vault(vault_id, dry_run=True)
        assert result['dry_run'] is True

    await svc.consolidate_vault(vault_id, dry_run=False)
    with pytest.raises(RateLimitExceededError):
        await svc.consolidate_vault(vault_id, dry_run=False)


@pytest.mark.asyncio
async def test_consolidate_rate_limit_disabled_via_config() -> None:
    clock = _FrozenClock()
    svc = _make_locks_service(enabled=False, clock=clock)
    vault_id = GLOBAL_VAULT_ID

    for _ in range(10):
        await svc.consolidate_vault(vault_id, dry_run=False)
