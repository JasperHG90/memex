"""F38 ConsolidationService.tick() integration tests.

Covers the AC-F38 invariants from RFC-010:
- AC-F38-1 step ordering (contradiction → reflection → prune) via call-order spy
- AC-F38-2 already-stale-only prune (assert F38 does NOT touch active units)
- Tick-summary row written with per-step counts
- Per-tick budget cap (oldest-first by AuditLog timestamp)
- No-op tick on empty diff (empty unit_ids → empty entity_ids → no calls)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    AuditLog,
    ConsolidationTick,
    ContentStatus,
    MemoryUnit,
    UnitEntity,
    Entity,
    Note,
    Vault,
)
from memex_core.services.consolidation import ConsolidationService

pytestmark = [pytest.mark.integration]


async def _seed_vault(session: AsyncSession) -> UUID:
    vault_id = uuid4()
    session.add(Vault(id=vault_id, name=f'v-{vault_id.hex[:8]}', description=''))
    await session.commit()
    return vault_id


async def _seed_note(session: AsyncSession, vault_id: UUID) -> UUID:
    note_id = uuid4()
    session.add(
        Note(
            id=note_id,
            vault_id=vault_id,
            content_hash=f'h-{note_id.hex[:8]}',
            original_text='seed note for F38 tests',
            filestore_path=None,
            assets=[],
        )
    )
    await session.commit()
    return note_id


async def _seed_unit_with_entity(
    session: AsyncSession,
    *,
    vault_id: UUID,
    note_id: UUID,
    status: ContentStatus = ContentStatus.ACTIVE,
) -> tuple[UUID, UUID]:
    unit_id = uuid4()
    entity_id = uuid4()
    session.add(
        MemoryUnit(
            id=unit_id,
            note_id=note_id,
            vault_id=vault_id,
            text='seeded fact',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            status=status,
            event_date=datetime.now(timezone.utc),
        )
    )
    session.add(Entity(id=entity_id, canonical_name=f'E-{entity_id.hex[:6]}'))
    session.add(UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=vault_id))
    await session.commit()
    return unit_id, entity_id


async def _emit_outcome_audit(
    session: AsyncSession,
    *,
    unit_id: UUID,
    vault_id: UUID,
    when: datetime | None = None,
) -> None:
    row = AuditLog(
        action='outcome.record',
        resource_type='memory_unit',
        resource_id=str(unit_id),
        details={'vault_id': str(vault_id), 'outcome': 'success'},
    )
    if when is not None:
        row.timestamp = when
    session.add(row)
    await session.commit()


def _make_service(metastore, *, contradiction_spy, reflection_spy, config) -> ConsolidationService:
    refl = MagicMock()
    refl.reflect_batch = reflection_spy
    return ConsolidationService(
        metastore=metastore,
        config=config,
        reflection=refl,
        contradiction=contradiction_spy,
    )


@pytest.mark.asyncio
async def test_runs_contradiction_then_reflection_then_prune(metastore, memex_config, session):
    """AC-F38-1: tick() invokes contradiction BEFORE reflection BEFORE prune."""
    vault_id = await _seed_vault(session)
    note_id = await _seed_note(session, vault_id)
    # One stale unit so prune is reachable.
    unit_id, _entity_id = await _seed_unit_with_entity(
        session, vault_id=vault_id, note_id=note_id, status=ContentStatus.STALE
    )
    await _emit_outcome_audit(session, unit_id=unit_id, vault_id=vault_id)

    call_order: list[str] = []

    async def _record_contradiction(**_kwargs):
        call_order.append('contradiction')

    async def _record_reflection(_requests):
        call_order.append('reflection')
        return []

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock(side_effect=_record_contradiction)
    reflection_spy = AsyncMock(side_effect=_record_reflection)

    # Patch prune_stale_evidence at the module-import location used by tick().
    from memex_core.services import consolidation as cm

    async def _record_prune(*_a, **_kw):
        call_order.append('prune')
        return set()

    original = cm.prune_stale_evidence
    cm.prune_stale_evidence = _record_prune  # type: ignore
    try:
        svc = _make_service(
            metastore,
            contradiction_spy=contradiction_spy,
            reflection_spy=reflection_spy,
            config=memex_config,
        )
        await svc.tick(vault_id)
    finally:
        cm.prune_stale_evidence = original  # type: ignore

    assert call_order == ['contradiction', 'reflection', 'prune'], (
        f'AC-F38-1 step ordering violated: got {call_order}'
    )


@pytest.mark.asyncio
async def test_does_not_prune_active_units(metastore, memex_config, session):
    """AC-F38-2: tick() must NOT call prune_stale_evidence when no units are STALE."""
    vault_id = await _seed_vault(session)
    note_id = await _seed_note(session, vault_id)
    unit_id, _entity_id = await _seed_unit_with_entity(
        session, vault_id=vault_id, note_id=note_id, status=ContentStatus.ACTIVE
    )
    await _emit_outcome_audit(session, unit_id=unit_id, vault_id=vault_id)

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock()
    reflection_spy = AsyncMock(return_value=[])

    from memex_core.services import consolidation as cm

    prune_calls = 0

    async def _track_prune(*_a, **_kw):
        nonlocal prune_calls
        prune_calls += 1
        return set()

    original = cm.prune_stale_evidence
    cm.prune_stale_evidence = _track_prune  # type: ignore
    try:
        svc = _make_service(
            metastore,
            contradiction_spy=contradiction_spy,
            reflection_spy=reflection_spy,
            config=memex_config,
        )
        result = await svc.tick(vault_id)
    finally:
        cm.prune_stale_evidence = original  # type: ignore

    assert prune_calls == 0, 'F38 must NOT prune ACTIVE units (AC-F38-2 invariant)'
    assert result['stale_pruned'] == 0


@pytest.mark.asyncio
async def test_tick_summary_row_written_with_counts(metastore, memex_config, session):
    """Tick summary row carries per-step counts and a non-null completed_at."""
    vault_id = await _seed_vault(session)
    note_id = await _seed_note(session, vault_id)
    unit_id, _ = await _seed_unit_with_entity(
        session, vault_id=vault_id, note_id=note_id, status=ContentStatus.STALE
    )
    await _emit_outcome_audit(session, unit_id=unit_id, vault_id=vault_id)

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock()
    reflection_spy = AsyncMock(return_value=[])

    from memex_core.services import consolidation as cm

    async def _noop_prune(*_a, **_kw):
        return set()

    original = cm.prune_stale_evidence
    cm.prune_stale_evidence = _noop_prune  # type: ignore
    try:
        svc = _make_service(
            metastore,
            contradiction_spy=contradiction_spy,
            reflection_spy=reflection_spy,
            config=memex_config,
        )
        result = await svc.tick(vault_id)
    finally:
        cm.prune_stale_evidence = original  # type: ignore

    async with metastore.session() as s:
        rows = (
            await s.exec(select(ConsolidationTick).where(ConsolidationTick.vault_id == vault_id))
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.completed_at is not None, 'completed_at NULL means in-progress; tick() must stamp it'
    assert row.units_processed == 1
    assert row.entities_reflected == 1
    assert row.contradictions_run == 1
    assert row.stale_pruned == 1
    assert row.error is None
    assert result['tick_id'] == str(row.id)


@pytest.mark.asyncio
async def test_no_op_tick_on_empty_diff(metastore, memex_config, session):
    """An empty diff still writes a tick row (with zero counts) and skips all calls."""
    vault_id = await _seed_vault(session)

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock()
    reflection_spy = AsyncMock(return_value=[])

    svc = _make_service(
        metastore,
        contradiction_spy=contradiction_spy,
        reflection_spy=reflection_spy,
        config=memex_config,
    )
    result = await svc.tick(vault_id)

    contradiction_spy.detect_contradictions.assert_not_called()
    reflection_spy.assert_not_called()
    assert result['units_processed'] == 0
    assert result['entities_reflected'] == 0
    assert result['contradictions_run'] == 0


@pytest.mark.asyncio
async def test_500_unit_cap_oldest_first(metastore, memex_config, session):
    """Per-tick budget caps at 500 units; oldest-first by AuditLog.timestamp."""
    vault_id = await _seed_vault(session)
    note_id = await _seed_note(session, vault_id)

    base = datetime.now(timezone.utc) - timedelta(days=1)
    seeded_ids: list[UUID] = []
    for i in range(7):
        uid, _ = await _seed_unit_with_entity(
            session, vault_id=vault_id, note_id=note_id, status=ContentStatus.ACTIVE
        )
        seeded_ids.append(uid)
        await _emit_outcome_audit(
            session,
            unit_id=uid,
            vault_id=vault_id,
            when=base + timedelta(seconds=i),
        )

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock()
    reflection_spy = AsyncMock(return_value=[])

    svc = _make_service(
        metastore,
        contradiction_spy=contradiction_spy,
        reflection_spy=reflection_spy,
        config=memex_config,
    )
    # Budget = 3; expect the 3 oldest (seeded_ids[0..2]).
    async with metastore.session() as s:
        selected = await svc.select_diff_units(s, vault_id, last_tick_timestamp=None, budget=3)
    assert selected == seeded_ids[:3], (
        f'Expected oldest-3 ordering; got {selected} vs {seeded_ids[:3]}'
    )
