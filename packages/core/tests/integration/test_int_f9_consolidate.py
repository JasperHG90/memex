"""TC-23-5 — memex_memory_consolidate(vault_id, dry_run) end-to-end (F9 ship).

3 tests:
- test_dry_run_returns_preview_without_writes: seeds 5 candidates that match
  the predicate + 1 negative control (recently created), runs `consolidate_vault`
  with `dry_run=True`, asserts (a) candidate count == 5 (negative excluded),
  (b) no rows written to maintenance_proposals, (c) no flips to is_deprioritized.
- test_writes_deprioritize_and_proposal_per_unit: same seed, `dry_run=False`,
  asserts (a) every candidate has is_deprioritized=True, (b) one
  MaintenanceProposal per candidate with rule_name='consolidate_vault_low_mw',
  status='resolved', source='rule', evidence['resolved_by'] correct.
- test_branch_b_fallback_when_table_missing: monkeypatches the
  has-table check to return False; asserts dry_run-shape result and
  no DB writes happen even with dry_run=False (defensive fallback).

Uses the F4 UnitsService for the deprioritize plumbing (real service, real DB).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.sql_models import (
    MaintenanceProposal,
    MemoryUnit,
    Note,
)
from memex_core.services.locks import LocksService
from memex_core.services.reflection import ReflectionService
from memex_core.services.units import UnitsService

pytestmark = [pytest.mark.integration]


@pytest.fixture
def consolidate_locks_service(metastore, filestore, memex_config) -> LocksService:
    """LocksService with real metastore + real UnitsService + mocked engines.

    Real UnitsService so the F4 deprioritize plumbing actually runs against
    the testcontainer DB. The contradiction + reflection mocks are unused by
    `consolidate_vault` but kept for symmetry with the constructor signature.
    """
    contradiction = MagicMock(spec=ContradictionEngine)
    contradiction.detect_contradictions = AsyncMock(return_value=None)

    reflection = MagicMock(spec=ReflectionService)
    reflection.reflect_batch = AsyncMock(return_value=[])

    units = UnitsService(metastore=metastore, filestore=filestore, config=memex_config)

    svc = LocksService(
        metastore=metastore,
        config=memex_config,
        reflection=reflection,
        contradiction=contradiction,
        units=units,
    )
    return svc


async def _seed_candidate_units(
    session: AsyncSession,
    *,
    vault_id: uuid.UUID,
    count: int,
    success_co: int = 1,
    failure_co: int = 9,
    age_days: int = 35,
) -> list[uuid.UUID]:
    """Seed N MemoryUnits matching the consolidate predicate.

    Default counters give mw_score = (1+1)/(1+9+2) = 0.166... < 0.35.
    Default age 35d > 30d threshold. is_deprioritized defaults to False.
    """
    note_id = uuid.uuid4()
    session.add(
        Note(
            id=note_id,
            content_hash=f'h-{note_id.hex[:6]}',
            vault_id=vault_id,
            original_text='seed',
        )
    )
    await session.flush()
    unit_ids: list[uuid.UUID] = []
    old_ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    for _ in range(count):
        uid = uuid.uuid4()
        unit = MemoryUnit(
            id=uid,
            note_id=note_id,
            text='low-mw seed',
            fact_type='world',
            vault_id=vault_id,
            embedding=[0.1] * 384,
            event_date=old_ts,
            success_co_count=success_co,
            failure_co_count=failure_co,
            is_deprioritized=False,
        )
        session.add(unit)
        unit_ids.append(uid)
    await session.commit()
    conn = await session.connection()
    await conn.execute(
        sqlalchemy.text('UPDATE memory_units SET created_at = :ts WHERE id = ANY(:ids)'),
        {'ts': old_ts, 'ids': unit_ids},
    )
    await session.commit()
    return unit_ids


async def _seed_negative_control_unit(
    session: AsyncSession,
    *,
    vault_id: uuid.UUID,
) -> uuid.UUID:
    """Seed a unit that should NOT be picked: recent created_at."""
    note_id = uuid.uuid4()
    session.add(
        Note(
            id=note_id,
            content_hash=f'h-neg-{note_id.hex[:6]}',
            vault_id=vault_id,
            original_text='recent',
        )
    )
    await session.flush()
    uid = uuid.uuid4()
    session.add(
        MemoryUnit(
            id=uid,
            note_id=note_id,
            text='recent low-mw',
            fact_type='world',
            vault_id=vault_id,
            embedding=[0.1] * 384,
            event_date=datetime.now(timezone.utc),
            success_co_count=1,
            failure_co_count=9,
            is_deprioritized=False,
        )
    )
    await session.commit()
    return uid


async def test_dry_run_returns_preview_without_writes(
    consolidate_locks_service: LocksService,
    session: AsyncSession,
) -> None:
    vault_id = GLOBAL_VAULT_ID
    candidates = await _seed_candidate_units(session, vault_id=vault_id, count=5)
    negative = await _seed_negative_control_unit(session, vault_id=vault_id)

    result = await consolidate_locks_service.consolidate_vault(vault_id, dry_run=True)

    assert result['vault_id'] == str(vault_id)
    assert result['dry_run'] is True
    assert result['candidates'] == 5
    assert result['units_deprioritized'] == 0
    assert result['proposals_written'] == 0

    proposals = list((await session.exec(select(MaintenanceProposal))).all())
    assert proposals == [], 'dry_run must not write proposals'

    for uid in candidates + [negative]:
        unit = await session.get(MemoryUnit, uid)
        assert unit is not None
        assert unit.is_deprioritized is False, 'dry_run must not flip is_deprioritized'


async def test_writes_deprioritize_and_proposal_per_unit(
    consolidate_locks_service: LocksService,
    session: AsyncSession,
) -> None:
    vault_id = GLOBAL_VAULT_ID
    candidates = await _seed_candidate_units(session, vault_id=vault_id, count=5)
    negative = await _seed_negative_control_unit(session, vault_id=vault_id)

    result = await consolidate_locks_service.consolidate_vault(
        vault_id, dry_run=False, actor='test-actor'
    )

    assert result['vault_id'] == str(vault_id)
    assert result['dry_run'] is False
    assert result['candidates'] == 5
    assert result['units_deprioritized'] == 5
    assert result['proposals_written'] == 5

    for uid in candidates:
        await session.refresh(await session.get(MemoryUnit, uid))
        unit = await session.get(MemoryUnit, uid)
        assert unit is not None
        assert unit.is_deprioritized is True, f'unit {uid} should be deprioritized'

    neg_unit = await session.get(MemoryUnit, negative)
    assert neg_unit is not None
    assert neg_unit.is_deprioritized is False, 'negative control must not be touched'

    proposals = list((await session.exec(select(MaintenanceProposal))).all())
    assert len(proposals) == 5
    proposal_targets = {uuid.UUID(p.target_id) for p in proposals}
    assert proposal_targets == set(candidates)
    for p in proposals:
        assert p.rule_name == 'consolidate_vault_low_mw'
        assert str(p.status) == 'resolved'
        assert str(p.source) == 'rule'
        assert str(p.lint_type) == 'quality'
        assert p.target_type == 'memory_unit'
        assert p.evidence['resolved_by'] == 'memex_memory_consolidate'
        assert p.evidence['actor'] == 'test-actor'
        assert p.resolved_at is not None
        # Issue #34 — first-class resolved_by column populated alongside
        # resolved_at on the F9 consolidate resolution path.
        assert p.resolved_by == 'test-actor'


async def test_branch_b_fallback_when_table_missing(
    consolidate_locks_service: LocksService,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `inspect(engine).has_table('maintenance_proposals')` is False,
    consolidate_vault still flips is_deprioritized but skips the proposal
    write (defensive fallback for pre-F6 deployments)."""
    vault_id = GLOBAL_VAULT_ID
    candidates = await _seed_candidate_units(session, vault_id=vault_id, count=2)

    async def _fake_has_table(self: LocksService, sa_inspect: object) -> bool:
        return False

    monkeypatch.setattr(LocksService, '_has_maintenance_proposals_table', _fake_has_table)

    result = await consolidate_locks_service.consolidate_vault(vault_id, dry_run=False)
    assert result['units_deprioritized'] == 2
    assert result['proposals_written'] == 0

    proposals = list((await session.exec(select(MaintenanceProposal))).all())
    assert proposals == [], 'Branch B must skip proposal writes when table missing'

    for uid in candidates:
        unit = await session.get(MemoryUnit, uid)
        assert unit is not None
        assert unit.is_deprioritized is True, 'deprioritize still happens in Branch B'
