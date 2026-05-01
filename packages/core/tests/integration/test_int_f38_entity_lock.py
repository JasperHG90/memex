"""F38 ConsolidationService — F9 per-entity advisory-lock integration (CRIT-002).

Phase-3 adversarial review CRITICAL-002 found that F38's tick bypassed F9's
``acquire_entity_lock`` and could race with ``memex_memory_reconsolidate`` on
the same MentalModel. These tests lock the contract that:

1. F38's per-entity loop holds the F9 advisory lock for the duration of work
   on that entity (release on success AND on exception).
2. Concurrent F9 ``LocksService.reconsolidate_entity`` and F38 ``tick``
   serialize on the same entity (no overlap window).
3. When the lock cannot be acquired within the timeout, F38 defers that
   entity (skip-and-log) instead of blocking the whole tick.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    AuditLog,
    ContentStatus,
    Entity,
    MemoryUnit,
    Note,
    UnitEntity,
    Vault,
)
from memex_core.services.consolidation import ConsolidationService
from memex_core.services.locks import (
    acquire_entity_lock,
)

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
            original_text='seed note for F38 lock tests',
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
) -> None:
    session.add(
        AuditLog(
            action='outcome.record',
            resource_type='memory_unit',
            resource_id=str(unit_id),
            details={'vault_id': str(vault_id), 'outcome': 'success'},
        )
    )
    await session.commit()


def _make_service(
    metastore,
    *,
    contradiction_spy,
    reflection_spy,
    config,
    entity_lock_timeout_seconds: float = 5.0,
) -> ConsolidationService:
    refl = MagicMock()
    refl.reflect_batch = reflection_spy
    return ConsolidationService(
        metastore=metastore,
        config=config,
        reflection=refl,
        contradiction=contradiction_spy,
        entity_lock_timeout_seconds=entity_lock_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_tick_skips_entity_when_lock_already_held(
    metastore, memex_config, session, postgres_uri
):
    """If another process holds the F9 lock, F38 must skip-and-log (defer)
    that entity, not block the whole tick."""
    vault_id = await _seed_vault(session)
    note_id = await _seed_note(session, vault_id)
    unit_a, entity_a = await _seed_unit_with_entity(session, vault_id=vault_id, note_id=note_id)
    unit_b, entity_b = await _seed_unit_with_entity(session, vault_id=vault_id, note_id=note_id)
    await _emit_outcome_audit(session, unit_id=unit_a, vault_id=vault_id)
    await _emit_outcome_audit(session, unit_id=unit_b, vault_id=vault_id)

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock()
    reflection_spy = AsyncMock(return_value=[])

    # Coerce SQLAlchemy DSN to plain postgresql:// for asyncpg.
    asyncpg_dsn = postgres_uri.replace('+asyncpg', '')

    # Hold the F9 lock on entity_a from a sibling task; F38 should defer it.
    holder_acquired = asyncio.Event()
    holder_release = asyncio.Event()

    async def _hold_lock_on_entity_a():
        async with acquire_entity_lock(asyncpg_dsn, entity_a, timeout_seconds=2.0):
            holder_acquired.set()
            await holder_release.wait()

    holder_task = asyncio.create_task(_hold_lock_on_entity_a())
    await asyncio.wait_for(holder_acquired.wait(), timeout=5.0)

    try:
        svc = _make_service(
            metastore,
            contradiction_spy=contradiction_spy,
            reflection_spy=reflection_spy,
            config=memex_config,
            entity_lock_timeout_seconds=0.5,  # short for fast skip
        )
        result = await svc.tick(vault_id)
    finally:
        holder_release.set()
        await holder_task

    # entity_a was deferred; entity_b was processed.
    assert result['entities_deferred'] == 1, result
    assert result['entities_reflected'] == 1, result
    # Reflection should have been called exactly once, for entity_b.
    assert reflection_spy.await_count == 1
    called_requests = reflection_spy.await_args_list[0].args[0]
    assert len(called_requests) == 1
    assert called_requests[0].entity_id == entity_b


@pytest.mark.asyncio
async def test_concurrent_tick_and_reconsolidate_serialize_on_same_entity(
    metastore, memex_config, session, postgres_uri
):
    """Two concurrent invocations on the same entity must NOT overlap. The
    second waiter must observe the first's lock until release."""
    vault_id = await _seed_vault(session)
    note_id = await _seed_note(session, vault_id)
    unit_id, entity_id = await _seed_unit_with_entity(session, vault_id=vault_id, note_id=note_id)
    await _emit_outcome_audit(session, unit_id=unit_id, vault_id=vault_id)

    asyncpg_dsn = postgres_uri.replace('+asyncpg', '')

    # Reflection spy tracks overlap: each call records (start, end). Sleep a
    # bit inside the body so concurrent calls would overlap if the lock were
    # missing.
    overlaps: list[tuple[float, float]] = []
    in_flight = 0
    max_in_flight = 0
    enter_lock = asyncio.Lock()

    async def _reflection_body(_requests):
        nonlocal in_flight, max_in_flight
        async with enter_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(0.3)
        end = asyncio.get_event_loop().time()
        overlaps.append((start, end))
        async with enter_lock:
            in_flight -= 1
        return []

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock()
    reflection_spy = AsyncMock(side_effect=_reflection_body)

    svc = _make_service(
        metastore,
        contradiction_spy=contradiction_spy,
        reflection_spy=reflection_spy,
        config=memex_config,
        entity_lock_timeout_seconds=10.0,
    )

    # Mimic an external `memex_memory_reconsolidate` by holding the same
    # advisory lock and doing equivalent work in parallel with `tick()`.
    async def _external_reconsolidate():
        async with acquire_entity_lock(asyncpg_dsn, entity_id, timeout_seconds=10.0):
            await _reflection_body(None)

    task_a = asyncio.create_task(svc.tick(vault_id))
    # Slight stagger so the contention is real but deterministic.
    await asyncio.sleep(0.05)
    task_b = asyncio.create_task(_external_reconsolidate())

    result, _ = await asyncio.gather(task_a, task_b)

    assert max_in_flight == 1, (
        f'F38 tick and external reconsolidate ran concurrently on the same '
        f'entity (max_in_flight={max_in_flight}); F9 lock was bypassed.'
    )
    # Verify intervals do not overlap (start of one >= end of other).
    a, b = overlaps
    assert a[1] <= b[0] or b[1] <= a[0], f'reflection intervals overlap: {a} vs {b}; lock not held'
    assert result['entities_reflected'] == 1
    assert result['entities_deferred'] == 0


@pytest.mark.asyncio
async def test_lock_releases_on_exception_in_tick_body(
    metastore, memex_config, session, postgres_uri
):
    """If reflection raises mid-tick on entity X, the F9 advisory lock for X
    must still be released (context-manager guarantee). A second acquire
    after the tick succeeds immediately."""
    vault_id = await _seed_vault(session)
    note_id = await _seed_note(session, vault_id)
    unit_id, entity_id = await _seed_unit_with_entity(session, vault_id=vault_id, note_id=note_id)
    await _emit_outcome_audit(session, unit_id=unit_id, vault_id=vault_id)

    asyncpg_dsn = postgres_uri.replace('+asyncpg', '')

    class _Boom(RuntimeError):
        pass

    contradiction_spy = MagicMock()
    contradiction_spy.detect_contradictions = AsyncMock()
    reflection_spy = AsyncMock(side_effect=_Boom('boom'))

    svc = _make_service(
        metastore,
        contradiction_spy=contradiction_spy,
        reflection_spy=reflection_spy,
        config=memex_config,
        entity_lock_timeout_seconds=2.0,
    )

    result = await svc.tick(vault_id)

    # The error should be captured in the tick row; lock must still be free.
    assert result['error'] is not None
    assert 'boom' in result['error'] or '_Boom' in result['error']

    # If the lock leaked, this acquire would block until our short timeout.
    async with acquire_entity_lock(asyncpg_dsn, entity_id, timeout_seconds=0.5):
        pass  # acquired without timeout — lock was released on exception
