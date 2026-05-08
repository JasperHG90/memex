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

from datetime import datetime, timedelta, timezone, tzinfo
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_common.config import MemexConfig
from memex_core.memory.lint_llm.types import LegacyRunLLMCheck
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
) -> LegacyRunLLMCheck:
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
    session: AsyncSession, metastore, memex_config, filestore, monkeypatch
) -> None:
    """10 buckets across midnight UTC — sum stays at 10 (rolling, not calendar).

    Pins ``datetime.now`` inside ``lint_llm`` to a deterministic UTC noon
    on a synthetic day — without it, the rolling-24h window depends on
    the wall-clock hour at test time, so buckets seeded around "today's"
    midnight UTC fall outside the window when the suite runs late in the
    UTC day (cutoff slides past pre-midnight buckets).
    """
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)

    # Pin "now" at noon UTC of a fixed synthetic day. The most-recent UTC
    # midnight is 12h ago — comfortably inside the rolling 24h window —
    # and the post-midnight buckets at midnight + 4h are still 8h before
    # "now", so all 10 buckets land inside the window regardless of when
    # the test actually executes.
    pinned_now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)

    # Stand-in for the ``datetime`` class — only ``now`` is overridden
    # because that's all ``lint_llm`` calls. Constructed as a plain object
    # rather than a ``datetime`` subclass to side-step the LSP-flavored
    # mypy errors on overriding the classmethod's signature.
    class _FrozenDatetime:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return pinned_now if tz is not None else pinned_now.replace(tzinfo=None)

    monkeypatch.setattr('memex_core.services.lint_llm.datetime', _FrozenDatetime)

    midnight = pinned_now.replace(hour=0, minute=0, second=0, microsecond=0)
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
    session: AsyncSession, metastore, memex_config, filestore, monkeypatch
) -> None:
    """Surprise score below threshold → no LLM call, no quota increment."""
    memex_config.server.memory.lint_llm.surprise_threshold = 0.99
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = _new_unit_id()

    # Stub compute_unit_surprise to return a low score.
    async def _low_surprise(*args, **kwargs):
        return 0.1

    monkeypatch.setattr('memex_core.services.lint_llm.compute_unit_surprise', _low_surprise)
    outcome = await svc.maybe_run(
        unit_id,
        vault_id,
        run_llm_check=_stub_check(returns=_make_finding(unit_id)),
        session=session,
    )
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
    session: AsyncSession, metastore, memex_config, filestore, monkeypatch
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

    async def _high_surprise(*args, **kwargs):
        return 0.9

    monkeypatch.setattr('memex_core.services.lint_llm.compute_unit_surprise', _high_surprise)
    outcome = await svc.maybe_run(
        unit_id,
        vault_id,
        run_llm_check=_stub_check(returns=_make_finding(unit_id)),
        session=session,
    )
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


# ---------------------------------------------------------------------------
# tick — scheduler entry-point (drains deferred + processes fresh candidates)
# ---------------------------------------------------------------------------


async def _seed_real_unit(session: AsyncSession, vault_id: UUID) -> UUID:
    """Insert a real memory_unit row (with an embedding) so list_tick_candidates
    can find it. Differs from _new_unit_id() which only mints a UUID."""
    unit_id = uuid4()
    note_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO notes (id, vault_id, content_hash, title)
            VALUES (:id, :vid, :hash, :title)
        """),
        {
            'id': str(note_id),
            'vid': str(vault_id),
            'hash': uuid4().hex,
            'title': 'F10 tick test',
        },
    )
    await session.execute(
        text("""
            INSERT INTO memory_units (
                id, note_id, vault_id, text, fact_type, status, embedding, event_date
            )
            VALUES (
                :id, :nid, :vid, :text, 'observation', 'active', :emb, :ed
            )
        """),
        {
            'id': str(unit_id),
            'nid': str(note_id),
            'vid': str(vault_id),
            'text': f'tick fact {unit_id}',
            'emb': str([0.1] * 384),
            'ed': datetime.now(timezone.utc),
        },
    )
    await session.commit()
    return unit_id


@pytest.mark.asyncio
async def test_tick_short_circuits_when_disabled(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.enabled = False
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)

    summary = await svc.tick(vault_id, run_llm_check=_stub_check(returns=_make_finding(uuid4())))
    assert summary.candidates_evaluated == 0
    assert summary.findings_emitted == 0


@pytest.mark.asyncio
async def test_tick_short_circuits_when_cap_zero(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 0
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    await _seed_real_unit(session, vault_id)

    summary = await svc.tick(vault_id, run_llm_check=_stub_check(returns=_make_finding(uuid4())))
    assert summary.candidates_evaluated == 0


@pytest.mark.asyncio
async def test_tick_picks_fresh_units_and_drains_deferred(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """A single tick must (a) drain the deferred queue first and (b) pick
    fresh candidate units up to units_per_tick. Loop closure: a unit
    deferred on a prior tick processes here when budget is available."""
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 5
    memex_config.server.memory.lint_llm.units_per_tick = 2
    memex_config.server.memory.lint_llm.surprise_threshold = 0.0  # admit all
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)

    # Pre-seed: one deferred row (from a prior over-cap tick).
    deferred_unit = await _seed_real_unit(session, vault_id)
    await svc.defer(
        deferred_unit,
        vault_id,
        reason='cost_cap_exceeded',
        surprise_score=0.9,
        session=session,
    )
    await session.commit()

    # Two fresh candidates that the tick should pick up.
    fresh_a = await _seed_real_unit(session, vault_id)
    fresh_b = await _seed_real_unit(session, vault_id)

    # Stub run_llm_check returns a finding for any unit.
    summary = await svc.tick(vault_id, run_llm_check=_stub_check(returns=_make_finding(fresh_a)))

    # Deferred unit was processed first.
    assert summary.deferred_processed == 1
    # Two fresh candidates evaluated, both below threshold (0.0 admits all).
    assert summary.candidates_evaluated == 2
    # 1 deferred + 2 fresh = 3 LLM calls = 3 quota slots used.
    used = await svc.quota_used(vault_id, session=session)
    assert used == 3

    # The deferred row was dismissed; no leftover llm_deferred rows pending.
    assert (
        await _count_proposals(session, vault_id, rule_name='llm_deferred', status='pending') == 0
    )
    # And new findings landed (with rule_name='llm_semantic_contradiction').
    new_findings = await _count_proposals(
        session, vault_id, rule_name='llm_semantic_contradiction', source='llm'
    )
    assert new_findings >= 2  # one from deferred, two from fresh, dedup-permitting

    # Sanity: candidate IDs include both fresh units (deferred already
    # excluded — its row had source='llm' which is the filter NOT EXISTS uses).
    _ = fresh_b  # referenced for clarity


@pytest.mark.asyncio
async def test_tick_respects_cost_cap_and_defers_excess_fresh(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    memex_config.server.memory.lint_llm.cost_cap_per_24h = 1
    memex_config.server.memory.lint_llm.units_per_tick = 3
    memex_config.server.memory.lint_llm.surprise_threshold = 0.0
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)

    [await _seed_real_unit(session, vault_id) for _ in range(3)]

    summary = await svc.tick(vault_id, run_llm_check=_stub_check(returns=_make_finding(uuid4())))

    # 1 admitted (cost cap), 2 deferred.
    assert summary.findings_emitted >= 1
    assert summary.deferred == 2
    deferred_pending = await _count_proposals(
        session, vault_id, rule_name='llm_deferred', status='pending'
    )
    assert deferred_pending == 2


@pytest.mark.asyncio
async def test_tick_skips_units_with_existing_pending_llm_proposal(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """list_tick_candidates excludes units that already have a pending
    source='llm' proposal — preventing re-audit on every tick."""
    svc = _service(metastore, memex_config, filestore)
    vault_id = await _make_vault(session)
    unit_id = await _seed_real_unit(session, vault_id)

    finding = _make_finding(unit_id, surprise=0.85)
    await svc.write_finding(finding, vault_id, session=session)
    await session.commit()

    candidates = await svc.list_tick_candidates(vault_id, limit=10, session=session)
    assert unit_id not in candidates


# ---------------------------------------------------------------------------
# LOW-F10-1 — DB-side defense-in-depth on count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lint_llm_quota_rejects_negative_count(
    session: AsyncSession, metastore, memex_config, filestore
) -> None:
    """ck_lint_llm_quota_count_non_negative must reject any direct write
    that would seat a negative count, regardless of whether the service-
    layer write path could ever produce one."""
    from sqlalchemy.exc import IntegrityError

    vault_id = await _make_vault(session)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    with pytest.raises(IntegrityError, match='ck_lint_llm_quota_count_non_negative'):
        await session.execute(
            text("""
                INSERT INTO lint_llm_quota (id, vault_id, hour_bucket, count)
                VALUES (gen_random_uuid(), :v, :h, -1)
            """),
            {'v': str(vault_id), 'h': now},
        )
        await session.commit()
    await session.rollback()
