"""V18 integration tests — CAS UPDATE correctness + asyncio.Lock semantics.

Tests against a real Postgres instance (testcontainers via the integration
fixtures) to validate that:

1. Phase 5 CAS UPDATE applies cleanly when the in-memory version matches
   the row version, and rejects (returns False, doesn't mutate the row)
   when the in-memory version is stale.
2. Two concurrent finalize calls on the same entity — one with fresh
   version, one with stale — serialize via Postgres row-level locking
   and end with exactly one committed update.
3. The pg_advisory advisory-lock surface no longer carries any
   ``hashtext('reflect:<entity_id>')`` keys after reflection runs — the
   pg_try_advisory_xact_lock call site has been removed.
4. Two reflections on different entities can proceed without serialising
   on each other's asyncio.Lock.

The asyncio.Lock unit-level semantics (identity, serialization,
parallelism, weak eviction) are covered in
``packages/core/tests/unit/memory/reflect/test_entity_lock_helper.py``.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import text as sql_text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import GLOBAL_VAULT_ID, MemexConfig
from memex_core.memory.reflect.entity_locks import get_entity_lock
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import Entity, MentalModel, Observation


def _make_embedder() -> MagicMock:
    mock = MagicMock()
    mock.encode.return_value = np.array([[0.1] * 384])
    return mock


async def _seed_entity_and_model(
    session: AsyncSession,
    *,
    version: int = 0,
    observations: list[dict] | None = None,
) -> tuple[Entity, MentalModel]:
    entity = Entity(canonical_name=f'Test {uuid4()}')
    session.add(entity)
    await session.flush()
    model = MentalModel(
        entity_id=entity.id,
        vault_id=GLOBAL_VAULT_ID,
        name=entity.canonical_name,
        observations=observations or [],
        version=version,
        embedding=[0.0] * 384,
    )
    session.add(model)
    await session.commit()
    await session.refresh(model)
    return entity, model


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase_5_cas_applies_when_version_matches(
    session: AsyncSession, memex_config: MemexConfig
):
    """Fresh version + matching DB row → CAS UPDATE applies (returns True)."""
    _entity, model = await _seed_entity_and_model(session, version=3)
    engine = ReflectionEngine(session=session, config=memex_config, embedder=_make_embedder())

    applied = await engine._phase_5_finalize(
        model,
        [Observation(title='T', content='C', evidence=[])],
        entity_summary='summary',
        entity_type='person',
    )

    assert applied is True
    assert model.version == 4
    await session.commit()

    # Reload from DB to confirm the row was written
    result = await session.execute(select(MentalModel).where(MentalModel.id == model.id))
    persisted = result.scalar_one()
    assert persisted.version == 4
    assert persisted.entity_metadata['description'] == 'summary'
    assert persisted.entity_metadata['observation_count'] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase_5_cas_abandons_when_version_stale(session_manager, memex_config: MemexConfig):
    """Stale in-memory version vs fresh DB row → CAS rejects (returns False), DB unchanged."""
    # Seed in setup session
    async with session_manager() as setup_session:
        _entity, model = await _seed_entity_and_model(setup_session, version=0)
        model_id = model.id

    # Connection A: load model, bump version 0→1 via CAS
    async with session_manager() as session_a:
        engine_a = ReflectionEngine(session_a, memex_config, _make_embedder())
        result_a = await session_a.execute(select(MentalModel).where(MentalModel.id == model_id))
        model_a = result_a.scalar_one()
        applied_a = await engine_a._phase_5_finalize(
            model_a,
            [Observation(title='WinnerObs', content='by A', evidence=[])],
            entity_summary='by A',
            entity_type='thing',
        )
        await session_a.commit()
    assert applied_a is True

    # Connection B: now load and try with the same stale in-memory version=0.
    # We synthesize this by force-setting model.version=0 on a freshly-loaded
    # row — represents the case where Connection B's earlier read saw v=0
    # before A's commit landed.
    async with session_manager() as session_b:
        engine_b = ReflectionEngine(session_b, memex_config, _make_embedder())
        result_b = await session_b.execute(select(MentalModel).where(MentalModel.id == model_id))
        model_b = result_b.scalar_one()
        # Simulate stale in-memory state
        model_b.version = 0
        applied_b = await engine_b._phase_5_finalize(
            model_b,
            [Observation(title='LoserObs', content='by B', evidence=[])],
            entity_summary='by B',
            entity_type='other',
        )
        # Read version inside the block to avoid DetachedInstanceError
        model_b_version_after = model_b.version

    assert applied_b is False
    # In-memory model should NOT have been mutated on the abandon path
    assert model_b_version_after == 0

    # The persisted row must still reflect Connection A's win
    async with session_manager() as verify_session:
        result = await verify_session.execute(select(MentalModel).where(MentalModel.id == model_id))
        persisted = result.scalar_one()
        assert persisted.version == 1
        assert persisted.entity_metadata['description'] == 'by A'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase_5_cas_serializes_concurrent_updates_via_row_lock(
    session_manager, memex_config: MemexConfig
):
    """Two concurrent CAS UPDATEs on the same row: row-lock serializes them, exactly one wins."""
    async with session_manager() as setup_session:
        _entity, model = await _seed_entity_and_model(setup_session, version=5)
        model_id = model.id

    async def attempt(label: str, hold_for: float) -> bool:
        async with session_manager() as s:
            engine = ReflectionEngine(s, memex_config, _make_embedder())
            result = await s.execute(select(MentalModel).where(MentalModel.id == model_id))
            mm = result.scalar_one()
            # Both sides see version=5; introduce a small async delay so both
            # start the UPDATE near-simultaneously.
            await asyncio.sleep(hold_for)
            applied = await engine._phase_5_finalize(
                mm,
                [Observation(title=label, content=label, evidence=[])],
                entity_summary=label,
                entity_type='t',
            )
            if applied:
                await s.commit()
            return applied

    results = await asyncio.gather(attempt('A', 0.01), attempt('B', 0.01))
    # Exactly one applied, exactly one abandoned
    assert results.count(True) == 1
    assert results.count(False) == 1

    async with session_manager() as verify_session:
        result = await verify_session.execute(select(MentalModel).where(MentalModel.id == model_id))
        persisted = result.scalar_one()
        assert persisted.version == 6  # exactly one bump
        # description records the winner's summary
        assert persisted.entity_metadata['description'] in {'A', 'B'}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_advisory_lock_acquired_during_reflection(
    session_manager, memex_config: MemexConfig
):
    """After V18-d, no pg_advisory lock with the 'reflect:<entity_id>' key is acquired.

    Pre-V18 the engine called ``pg_try_advisory_xact_lock(hashtext('reflect:<id>'))``;
    after V18-d that call site is gone. The check observes pg_locks via a
    side session to confirm no advisory entries with the engine's namespace
    appear during Phase 5.
    """
    async with session_manager() as setup_session:
        _entity, model = await _seed_entity_and_model(setup_session, version=0)

    async with session_manager() as engine_session, session_manager() as observer_session:
        engine = ReflectionEngine(engine_session, memex_config, _make_embedder())
        # Run phase 5 to exercise the post-V18-d code path
        await engine._phase_5_finalize(
            model,
            [Observation(title='x', content='x', evidence=[])],
            entity_summary='x',
            entity_type='t',
        )
        # Observer queries pg_locks for any advisory lock during the in-flight transaction
        result = await observer_session.execute(
            sql_text("SELECT COUNT(*) FROM pg_locks WHERE locktype='advisory'")
        )
        advisory_count = result.scalar_one()
        await engine_session.commit()

    # No advisory locks should appear from the reflection codepath.
    assert advisory_count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entity_lock_helper_distinguishes_locks_across_entities(
    session: AsyncSession, memex_config: MemexConfig
):
    """The asyncio.Lock helper returns distinct locks for distinct entity_ids
    even when used alongside real DB sessions — confirms the helper composes
    correctly with the integration test fixtures.
    """
    eid_a = uuid4()
    eid_b = uuid4()
    lock_a = await get_entity_lock(eid_a)
    lock_b = await get_entity_lock(eid_b)
    assert lock_a is not lock_b
    assert await get_entity_lock(eid_a) is lock_a
