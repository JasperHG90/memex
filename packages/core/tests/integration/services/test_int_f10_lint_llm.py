"""F10 — LintLLMService integration tests against real Postgres.

Covers RFC-006 §"Required Tests" rows for cost-cap semantics, defer-not-drop,
queue-cap eviction, vault isolation, and F8-parity for ``source='llm'``
findings. The DSPy lint signatures themselves land in subsequent commits;
these tests inject a stub ``run_llm_check`` so the orchestration is
independently verifiable.

Maps to ACs:
  * AC-F10-1 (surprise gate)
  * AC-F10-3 (defer-not-drop loop closure)
  * AC-F10-4 (deferred queue cap)
  * AC-F10-5 (F8 returns LLM findings)
  * AC-X-7 (vault scoping)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_common.config import MemexConfig
from memex_core.memory.sql_models import LintType, Vault
from memex_core.services.lint_llm import (
    LLMLintFinding,
    LintLLMService,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(metastore, config: MemexConfig, filestore) -> LintLLMService:
    return LintLLMService(metastore=metastore, filestore=filestore, config=config)


async def _make_vault(session: AsyncSession, name_prefix: str = 'F10') -> UUID:
    v = Vault(name=f'{name_prefix}-{uuid4().hex[:8]}')
    session.add(v)
    await session.commit()
    await session.refresh(v)
    return v.id


def _new_unit_id() -> UUID:
    """Return a fresh unit-id to use as a MaintenanceProposal target_id.

    The proposal table treats ``target_id`` as opaque text — no FK to
    memory_units — so tests that don't exercise ``compute_unit_surprise``
    don't need a backing row. Tests that DO exercise the surprise wrapper
    monkeypatch it (the wrapper is unit-tested independently).
    """
    return uuid4()


def _make_finding(unit_id: UUID, surprise: float = 0.85) -> LLMLintFinding:
    return LLMLintFinding(
        rule_name='llm_semantic_contradiction',
        check_type='semantic_contradiction',
        target_type='memory_unit',
        target_id=str(unit_id),
        suggested_action='Review for contradiction',
        surprise_score=surprise,
        explanation='unit X claims A; unit Y claims not-A',
        related_unit_ids=[str(uuid4())],
        lint_type=LintType.QUALITY,
    )


def _stub_check(
    *, returns: LLMLintFinding | None = None, raises: Exception | None = None
) -> Callable[[UUID, UUID, AsyncSession], Awaitable[LLMLintFinding | None]]:
    """Inject-able run_llm_check; defaults to returning ``None`` (no finding)."""

    async def _impl(unit_id: UUID, vault_id: UUID, session: AsyncSession) -> LLMLintFinding | None:
        if raises is not None:
            raise raises
        if returns is not None:
            return _make_finding(unit_id, surprise=returns.surprise_score)
        return None

    return _impl


async def _seed_quota_bucket(
    session: AsyncSession,
    vault_id: UUID,
    *,
    hour_bucket: datetime,
    count: int,
) -> None:
    await session.execute(
        text("""
            INSERT INTO lint_llm_quota (id, vault_id, hour_bucket, count)
            VALUES (gen_random_uuid(), :v, :h, :c)
        """),
        {'v': str(vault_id), 'h': hour_bucket, 'c': count},
    )
    await session.commit()


async def _count_quota_buckets(session: AsyncSession, vault_id: UUID) -> int:
    result = await session.execute(
        text('SELECT count(*) FROM lint_llm_quota WHERE vault_id = :v'),
        {'v': str(vault_id)},
    )
    return int(result.scalar() or 0)


async def _count_proposals(
    session: AsyncSession,
    vault_id: UUID,
    *,
    rule_name: str | None = None,
    status: str = 'pending',
    source: str | None = None,
) -> int:
    sql = 'SELECT count(*) FROM maintenance_proposals WHERE vault_id = :v AND status = :s'
    params: dict = {'v': str(vault_id), 's': status}
    if rule_name is not None:
        sql += ' AND rule_name = :r'
        params['r'] = rule_name
    if source is not None:
        sql += ' AND source = :src'
        params['src'] = source
    result = await session.execute(text(sql), params)
    return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# Quota — UPSERT correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_upsert_creates_then_increments(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)

    assert await svc.check_and_increment_quota(vault_id, session=session) is True
    await session.commit()
    assert await svc.quota_used(vault_id, session=session) == 1

    assert await svc.check_and_increment_quota(vault_id, session=session) is True
    await session.commit()
    assert await svc.quota_used(vault_id, session=session) == 2

    # Two increments in the same hour produce one row, count=2.
    assert await _count_quota_buckets(session, vault_id) == 1


# ---------------------------------------------------------------------------
# Quota — 24h rolling window math
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_window_excludes_buckets_older_than_24h(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # Old bucket (>24h) should not contribute.
    await _seed_quota_bucket(session, vault_id, hour_bucket=now - timedelta(hours=25), count=999)
    # Recent bucket (within 24h) contributes.
    await _seed_quota_bucket(session, vault_id, hour_bucket=now - timedelta(hours=1), count=3)

    used = await svc.quota_used(vault_id, session=session)
    assert used == 3


@pytest.mark.asyncio
async def test_quota_window_includes_partial_current_hour(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    await _seed_quota_bucket(session, vault_id, hour_bucket=now, count=4)

    used = await svc.quota_used(vault_id, session=session)
    assert used == 4


@pytest.mark.asyncio
async def test_quota_window_sums_last_24_buckets(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # 24 hourly buckets, 1 each.
    for h in range(24):
        await _seed_quota_bucket(session, vault_id, hour_bucket=now - timedelta(hours=h), count=1)

    used = await svc.quota_used(vault_id, session=session)
    assert used == 24


# ---------------------------------------------------------------------------
# Quota — cap enforcement at boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_allows_at_cap_minus_one(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 5
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    await _seed_quota_bucket(session, vault_id, hour_bucket=now, count=4)

    assert await svc.check_and_increment_quota(vault_id, session=session) is True
    await session.commit()
    # Now at cap — next call MUST be blocked.
    assert await svc.check_and_increment_quota(vault_id, session=session) is False


@pytest.mark.asyncio
async def test_quota_blocks_at_cap(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 5
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    await _seed_quota_bucket(session, vault_id, hour_bucket=now, count=5)

    assert await svc.check_and_increment_quota(vault_id, session=session) is False


@pytest.mark.asyncio
async def test_quota_blocks_above_cap(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 5
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    await _seed_quota_bucket(session, vault_id, hour_bucket=now, count=99)

    assert await svc.check_and_increment_quota(vault_id, session=session) is False


@pytest.mark.asyncio
async def test_quota_zero_cap_always_blocks(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 0
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)

    assert await svc.check_and_increment_quota(vault_id, session=session) is False


# ---------------------------------------------------------------------------
# Quota — vault isolation (AC-X-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_per_vault_independent(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 3
    svc = _service(metastore, memex_config, filestore)
    vault_a = await _make_vault(session, name_prefix='F10-vaultA')
    vault_b = await _make_vault(session, name_prefix='F10-vaultB')
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # Vault A is at cap.
    await _seed_quota_bucket(session, vault_a, hour_bucket=now, count=3)

    # Vault A blocks; Vault B has full budget.
    assert await svc.check_and_increment_quota(vault_a, session=session) is False
    assert await svc.check_and_increment_quota(vault_b, session=session) is True


# ---------------------------------------------------------------------------
# Rolling-window smoothing (RFC-006 §"Cost cap default")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_calendar_day_reset(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """10 buckets across midnight UTC — sum stays at 10 (rolling, not calendar)."""
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)

    # Anchor at most-recent UTC midnight; spread 10 buckets across it.
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for h in range(-5, 5):  # 5 before midnight, 5 after
        await _seed_quota_bucket(
            session, vault_id, hour_bucket=midnight + timedelta(hours=h), count=1
        )

    used = await svc.quota_used(vault_id, session=session)
    assert used == 10


# ---------------------------------------------------------------------------
# maybe_run — orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_run_skips_below_threshold(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """Surprise score below threshold → no LLM call, no quota increment."""
    memex_config.server.memory.lint_llm.surprise_threshold = 0.99
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = _new_unit_id()

    # Stub compute_unit_surprise to return a low score.
    from memex_core.services import lint_llm as lint_llm_module

    async def _low_surprise(*args, **kwargs):
        return 0.1

    monkeypatched = lint_llm_module.compute_unit_surprise
    lint_llm_module.compute_unit_surprise = _low_surprise
    try:
        outcome = await svc.maybe_run(
            unit_id,
            vault_id,
            run_llm_check=_stub_check(returns=_make_finding(unit_id)),
            session=session,
        )
    finally:
        lint_llm_module.compute_unit_surprise = monkeypatched
    await session.commit()

    assert outcome.skipped_below_threshold is True
    assert outcome.deferred is False
    assert outcome.finding_emitted is False
    assert await svc.quota_used(vault_id, session=session) == 0


@pytest.mark.asyncio
async def test_maybe_run_disabled_short_circuits(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.enabled = False
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = _new_unit_id()

    outcome = await svc.maybe_run(
        unit_id,
        vault_id,
        run_llm_check=_stub_check(returns=_make_finding(unit_id)),
        session=session,
    )
    assert outcome.skipped_disabled is True
    assert outcome.surprise_score is None  # never computed


@pytest.mark.asyncio
async def test_maybe_run_zero_cap_short_circuits(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """cost_cap_per_24h = 0 disables F10 entirely (RFC-006 decision #2)."""
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 0
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = _new_unit_id()

    outcome = await svc.maybe_run(
        unit_id,
        vault_id,
        run_llm_check=_stub_check(returns=_make_finding(unit_id)),
        session=session,
    )
    assert outcome.skipped_disabled is True


# ---------------------------------------------------------------------------
# defer-not-drop semantics (AC-F10-3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defers_when_cap_exceeded(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """Above-cap units are persisted as llm_deferred MaintenanceProposal rows."""
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 1
    memex_config.server.memory.lint_llm.surprise_threshold = 0.5
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = _new_unit_id()

    # Saturate the quota for this vault.
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await _seed_quota_bucket(session, vault_id, hour_bucket=now, count=1)

    from memex_core.services import lint_llm as lint_llm_module

    async def _high_surprise(*args, **kwargs):
        return 0.9

    monkeypatched = lint_llm_module.compute_unit_surprise
    lint_llm_module.compute_unit_surprise = _high_surprise
    try:
        outcome = await svc.maybe_run(
            unit_id,
            vault_id,
            run_llm_check=_stub_check(returns=_make_finding(unit_id)),
            session=session,
        )
    finally:
        lint_llm_module.compute_unit_surprise = monkeypatched
    await session.commit()

    assert outcome.deferred is True
    assert outcome.finding_emitted is False

    deferred_count = await _count_proposals(
        session, vault_id, rule_name='llm_deferred', status='pending'
    )
    assert deferred_count == 1


@pytest.mark.asyncio
async def test_deferred_units_processed_next_tick_when_quota_available(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """AC-F10-3 loop closure: deferral writes the queue AND the next tick
    actually picks the deferred unit up and produces a real source='llm'
    finding when budget is available.
    """
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 5
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = _new_unit_id()

    # Hand-deliver a deferred row to simulate a prior over-cap tick.
    await svc.defer(
        unit_id, vault_id, reason='cost_cap_exceeded', surprise_score=0.9, session=session
    )
    await session.commit()
    assert (
        await _count_proposals(session, vault_id, rule_name='llm_deferred', status='pending') == 1
    )

    # Next tick: budget available, deferred unit must process and emit a
    # source='llm' finding.
    processed = await svc.process_deferred(
        vault_id,
        run_llm_check=_stub_check(returns=_make_finding(unit_id)),
        session=session,
    )
    await session.commit()
    assert processed == 1

    # The deferred row was dismissed (resolved_at NOT NULL).
    assert (
        await _count_proposals(session, vault_id, rule_name='llm_deferred', status='pending') == 0
    )
    dismissed = await _count_proposals(
        session, vault_id, rule_name='llm_deferred', status='dismissed'
    )
    assert dismissed == 1

    # And a real LLM finding landed.
    llm_findings = await _count_proposals(
        session, vault_id, rule_name='llm_semantic_contradiction', source='llm'
    )
    assert llm_findings == 1


@pytest.mark.asyncio
async def test_process_deferred_no_op_when_quota_full(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 1
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = _new_unit_id()

    await svc.defer(
        unit_id, vault_id, reason='cost_cap_exceeded', surprise_score=0.9, session=session
    )
    # Saturate quota.
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    await _seed_quota_bucket(session, vault_id, hour_bucket=now, count=1)
    await session.commit()

    processed = await svc.process_deferred(
        vault_id,
        run_llm_check=_stub_check(returns=_make_finding(unit_id)),
        session=session,
    )
    assert processed == 0
    # Deferred row remains pending — defer-not-drop semantics across ticks.
    assert (
        await _count_proposals(session, vault_id, rule_name='llm_deferred', status='pending') == 1
    )


# ---------------------------------------------------------------------------
# Deferred queue cap (AC-F10-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deferred_queue_caps_per_vault(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """Once the deferred queue exceeds deferred_queue_cap, oldest entries are
    dismissed (non-destructive eviction) so the queue stays bounded."""
    memex_config.server.memory.lint_llm.deferred_queue_cap = 3
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)

    # Defer 5 distinct units with monotonically increasing created_at.
    unit_ids = [_new_unit_id() for _ in range(5)]
    for uid in unit_ids:
        await svc.defer(
            uid, vault_id, reason='cost_cap_exceeded', surprise_score=0.9, session=session
        )
    await session.commit()

    pending = await _count_proposals(session, vault_id, rule_name='llm_deferred', status='pending')
    dismissed = await _count_proposals(
        session, vault_id, rule_name='llm_deferred', status='dismissed'
    )
    # Cap=3, deferred 5 → 3 pending, 2 evicted-as-dismissed.
    assert pending == 3
    assert dismissed == 2


# ---------------------------------------------------------------------------
# F8 parity — source='llm' findings are visible via the read surface (AC-F10-5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f8_returns_llm_findings(
    session: AsyncSession, metastore, memex_config, filestore, api
) -> None:
    """A finding written by F10 surfaces unchanged through F8's read path,
    distinguishable only by source='llm'."""
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = _new_unit_id()

    finding = _make_finding(unit_id, surprise=0.85)
    inserted = await svc.write_finding(finding, vault_id, session=session)
    assert inserted is True
    await session.commit()

    page = await api.lint.get_findings(vault_id=vault_id)
    llm_findings = [f for f in page.findings if f.source == 'llm']
    assert len(llm_findings) == 1
    f = llm_findings[0]
    assert f.rule_name == 'llm_semantic_contradiction'
    assert f.target_id == str(unit_id)
    assert f.evidence['check_type'] == 'semantic_contradiction'
    assert f.evidence['surprise_score'] == 0.85
    assert f.evidence['explanation']  # non-empty
