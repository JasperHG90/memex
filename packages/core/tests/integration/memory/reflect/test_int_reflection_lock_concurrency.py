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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase_5_cas_via_entity_session_factory_persists_to_separate_session(
    session_manager, memex_config: MemexConfig
):
    """Drives ``_phase_5_finalize`` with an explicitly-supplied session
    from a factory that is independent of the engine's ``self.session``.
    Verifies the CAS UPDATE persists on the factory-opened session and
    a third observer session sees the write.
    """
    async with session_manager() as setup_session:
        _entity, model = await _seed_entity_and_model(setup_session, version=4)
        model_id = model.id

    async with session_manager() as orchestrator_session:
        engine = ReflectionEngine(
            orchestrator_session,
            memex_config,
            _make_embedder(),
            entity_session_factory=session_manager,
        )
        async with engine._entity_session() as factory_session:
            assert factory_session is not orchestrator_session, (
                'factory_session must be distinct from orchestrator_session'
            )
            result = await factory_session.execute(
                select(MentalModel).where(MentalModel.id == model_id)
            )
            loaded = result.scalar_one()
            applied = await engine._phase_5_finalize(
                loaded,
                [Observation(title='FromProd', content='via factory', evidence=[])],
                session=factory_session,
                entity_summary='via factory',
                entity_type='thing',
            )
            assert applied is True

    async with session_manager() as observer_session:
        result = await observer_session.execute(
            select(MentalModel).where(MentalModel.id == model_id)
        )
        persisted = result.scalar_one()
        assert persisted.version == 5
        assert persisted.entity_metadata['description'] == 'via factory'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_workers_different_entities_run_in_parallel(
    session_manager, memex_config: MemexConfig
):
    """Wall-clock parallelism gate: two reflections on different entities
    must not serialize on each other. Mocks DSPy phases with asyncio.sleep
    so the entire reflection takes ~sleep_for; gathered concurrently, the
    total wall-clock should be ~sleep_for (parallel) rather than
    ~2*sleep_for (serialised).
    """
    from unittest.mock import patch, AsyncMock
    from memex_core.memory.sql_models import MemoryUnit
    from memex_common.types import FactTypes

    async with session_manager() as setup_session:
        entity_a, model_a = await _seed_entity_and_model(setup_session, version=0)
        entity_b, model_b = await _seed_entity_and_model(setup_session, version=0)

    sleep_for = 0.5

    async def run_reflection(entity, model):
        async with session_manager() as orchestrator_session:
            engine = ReflectionEngine(
                orchestrator_session,
                memex_config,
                _make_embedder(),
                entity_session_factory=session_manager,
            )
            engine.lm = MagicMock()
            with (
                patch.object(engine, '_phase_1_seed', new_callable=AsyncMock) as mock_seed,
                patch.object(engine, '_phase_2_hunt', new_callable=AsyncMock) as mock_hunt,
                patch.object(engine, '_phase_3_validate', new_callable=AsyncMock) as mock_val,
                patch.object(engine, '_phase_4_compare', new_callable=AsyncMock) as mock_comp,
            ):

                async def _sleepy(*args, **kwargs):
                    await asyncio.sleep(sleep_for / 4)
                    return []

                async def _sleepy_compare(*args, **kwargs):
                    await asyncio.sleep(sleep_for / 4)
                    return ([Observation(title='T', content='C', evidence=[])], 'summary')

                mock_seed.side_effect = _sleepy
                mock_hunt.side_effect = _sleepy
                mock_val.side_effect = _sleepy
                mock_comp.side_effect = _sleepy_compare

                fake_memory = MemoryUnit(
                    id=uuid4(),
                    text='trigger',
                    vault_id=GLOBAL_VAULT_ID,
                    fact_type=FactTypes.WORLD,
                    embedding=[0.0] * 384,
                )
                await engine._reflect_entity_internal(
                    entity_id=entity.id,
                    mental_model=model,
                    entity=entity,
                    recent_memories=[fake_memory],
                    vault_id=GLOBAL_VAULT_ID,
                )

    loop = asyncio.get_event_loop()
    start = loop.time()
    await asyncio.gather(
        run_reflection(entity_a, model_a),
        run_reflection(entity_b, model_b),
    )
    elapsed = loop.time() - start
    # Both reflections run in ~sleep_for; parallel should be ≤ 1.4× sleep_for
    # (allow margin for non-LLM overhead — DB roundtrips, embedding calc).
    # Serialised would be ≥ 1.8 × sleep_for.
    assert elapsed < sleep_for * 1.5, (
        f'reflections on distinct entities should run in parallel; '
        f'elapsed={elapsed:.3f}s, sleep_for={sleep_for:.3f}s (parallel target <{sleep_for * 1.5:.3f}s)'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_long_running_transaction_during_llm_phases(
    session_manager, memex_config: MemexConfig
):
    """V18's primary motivation: no DB transaction should outlive an LLM call.
    Observer session polls pg_stat_activity for the engine's connection
    while a reflection is mid-LLM-phase; asserts no transaction has been
    open for more than 0.5s during the LLM-bound sleep.
    """
    from unittest.mock import patch, AsyncMock
    from memex_core.memory.sql_models import MemoryUnit
    from memex_common.types import FactTypes

    async with session_manager() as setup_session:
        entity, model = await _seed_entity_and_model(setup_session, version=0)

    sleep_for = 1.0
    observed_xact_durations: list[float] = []
    observation_stop = asyncio.Event()

    async def observer():
        async with session_manager() as obs_session:
            while not observation_stop.is_set():
                # xact_start is NULL for connections without an active xact
                result = await obs_session.execute(
                    sql_text(
                        'SELECT EXTRACT(EPOCH FROM (now() - xact_start)) '
                        'FROM pg_stat_activity '
                        'WHERE xact_start IS NOT NULL AND datname = current_database()'
                    )
                )
                for row in result:
                    if row[0] is not None:
                        observed_xact_durations.append(float(row[0]))
                await asyncio.sleep(0.1)

    async def run_reflection():
        async with session_manager() as orchestrator_session:
            engine = ReflectionEngine(
                orchestrator_session,
                memex_config,
                _make_embedder(),
                entity_session_factory=session_manager,
            )
            engine.lm = MagicMock()
            with (
                patch.object(engine, '_phase_1_seed', new_callable=AsyncMock) as mock_seed,
                patch.object(engine, '_phase_2_hunt', new_callable=AsyncMock) as mock_hunt,
                patch.object(engine, '_phase_3_validate', new_callable=AsyncMock) as mock_val,
                patch.object(engine, '_phase_4_compare', new_callable=AsyncMock) as mock_comp,
            ):

                async def _sleepy(*args, **kwargs):
                    await asyncio.sleep(sleep_for / 4)
                    return []

                async def _sleepy_compare(*args, **kwargs):
                    await asyncio.sleep(sleep_for / 4)
                    return ([Observation(title='T', content='C', evidence=[])], 'summary')

                mock_seed.side_effect = _sleepy
                mock_hunt.side_effect = _sleepy
                mock_val.side_effect = _sleepy
                mock_comp.side_effect = _sleepy_compare

                fake_memory = MemoryUnit(
                    id=uuid4(),
                    text='trigger',
                    vault_id=GLOBAL_VAULT_ID,
                    fact_type=FactTypes.WORLD,
                    embedding=[0.0] * 384,
                )
                await engine._reflect_entity_internal(
                    entity_id=entity.id,
                    mental_model=model,
                    entity=entity,
                    recent_memories=[fake_memory],
                    vault_id=GLOBAL_VAULT_ID,
                )
        observation_stop.set()

    await asyncio.gather(observer(), run_reflection())

    # The longest observed transaction duration during the reflection
    # should be much less than the total reflection wall-clock. Each LLM-
    # bound sleep is ~sleep_for/4 = 0.25s; transactions for DB ops are
    # at most a few ms. Setting the threshold at sleep_for/2 = 0.5s
    # leaves headroom for slow CI but still catches a regression that
    # held a transaction across all four LLM phases.
    if observed_xact_durations:
        max_xact = max(observed_xact_durations)
        assert max_xact < sleep_for / 2, (
            f'a transaction was held for {max_xact:.3f}s, exceeding the '
            f'{sleep_for / 2:.3f}s budget — V18 invariant "no DB transaction '
            f'spans an LLM call" is violated.'
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflect_entity_internal_via_factory_persists_full_pipeline(
    session_manager, memex_config: MemexConfig
):
    """Drive ``_reflect_entity_internal`` with the production-wired
    ``entity_session_factory`` so Phase 0, Phase 2, Phase 5 each open
    their own session. Mocks the LLM phases to keep the test deterministic.
    Asserts the final mental_model row is persisted and the orchestrator's
    self.session is *not* the channel through which it landed.
    """
    from unittest.mock import patch, AsyncMock

    async with session_manager() as setup_session:
        entity, model = await _seed_entity_and_model(setup_session, version=0)
        entity_id = entity.id
        model_id = model.id

    async with session_manager() as orchestrator_session:
        engine = ReflectionEngine(
            orchestrator_session,
            memex_config,
            _make_embedder(),
            entity_session_factory=session_manager,
        )
        engine.lm = MagicMock()

        # Mock the LLM phases to return deterministic content
        with (
            patch.object(engine, '_phase_1_seed', new_callable=AsyncMock) as mock_seed,
            patch.object(engine, '_phase_2_hunt', new_callable=AsyncMock) as mock_hunt,
            patch.object(engine, '_phase_3_validate', new_callable=AsyncMock) as mock_val,
            patch.object(engine, '_phase_4_compare', new_callable=AsyncMock) as mock_comp,
        ):
            mock_seed.return_value = []
            mock_hunt.return_value = []
            mock_val.return_value = []
            mock_comp.return_value = (
                [Observation(title='Prod', content='via per-entity sess', evidence=[])],
                'summary',
            )
            # Fabricate one memory so we don't hit the no-recent-memories
            # early-return path
            from memex_core.memory.sql_models import MemoryUnit
            from memex_common.types import FactTypes

            fake_memory = MemoryUnit(
                id=uuid4(),
                text='trigger',
                vault_id=GLOBAL_VAULT_ID,
                fact_type=FactTypes.WORLD,
                embedding=[0.0] * 384,
            )

            returned = await engine._reflect_entity_internal(
                entity_id=entity_id,
                mental_model=model,
                entity=entity,
                recent_memories=[fake_memory],
                vault_id=GLOBAL_VAULT_ID,
            )
            assert returned is model
            assert model.version == 1
            assert len(model.observations) == 1

    # Observer session must see the persisted state
    async with session_manager() as observer_session:
        result = await observer_session.execute(
            select(MentalModel).where(MentalModel.id == model_id)
        )
        persisted = result.scalar_one()
        assert persisted.version == 1
        assert persisted.entity_metadata['description'] == 'summary'
        assert len(persisted.observations) == 1
        assert persisted.observations[0]['title'] == 'Prod'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflect_entity_internal_returns_none_on_cas_abandon(
    session_manager, memex_config: MemexConfig
):
    """Round-4 CRITICAL regression guard: on CAS abandon, the engine's
    per-entity coroutine MUST return None — NOT the (unchanged) MentalModel.

    Returning the model would route the entity through
    ``services.reflection.queue_service.complete_reflection`` (which DELETES
    the queue row), losing the retry path. Returning None lets the service
    layer's else-branch fire ``mark_failed`` which flips the row back to
    FAILED so the next ``claim_reflection_queue_batch`` SKIP LOCKED tick
    re-picks the entity.
    """
    from unittest.mock import patch, AsyncMock
    from memex_core.memory.sql_models import MemoryUnit
    from memex_common.types import FactTypes
    from sqlalchemy import update as sa_update

    async with session_manager() as setup_session:
        entity, model = await _seed_entity_and_model(setup_session, version=3)
        entity_id = entity.id
        model_id = model.id

    # Simulate a concurrent winner that bumped version 3 -> 4 between our
    # _batch_get_or_create_models read (still sees version=3) and our
    # Phase 5 CAS UPDATE.
    async with session_manager() as winner_session:
        await winner_session.execute(
            sa_update(MentalModel).where(MentalModel.id == model_id).values(version=4)
        )
        await winner_session.commit()

    async with session_manager() as orchestrator_session:
        engine = ReflectionEngine(
            orchestrator_session,
            memex_config,
            _make_embedder(),
            entity_session_factory=session_manager,
        )
        engine.lm = MagicMock()
        with (
            patch.object(engine, '_phase_1_seed', new_callable=AsyncMock) as mock_seed,
            patch.object(engine, '_phase_2_hunt', new_callable=AsyncMock) as mock_hunt,
            patch.object(engine, '_phase_3_validate', new_callable=AsyncMock) as mock_val,
            patch.object(engine, '_phase_4_compare', new_callable=AsyncMock) as mock_comp,
        ):
            mock_seed.return_value = []
            mock_hunt.return_value = []
            mock_val.return_value = []
            mock_comp.return_value = (
                [Observation(title='Stale', content='will lose CAS', evidence=[])],
                'stale-summary',
            )

            fake_memory = MemoryUnit(
                id=uuid4(),
                text='trigger',
                vault_id=GLOBAL_VAULT_ID,
                fact_type=FactTypes.WORLD,
                embedding=[0.0] * 384,
            )

            # ``model`` is the in-memory copy that still has version=3.
            returned = await engine._reflect_entity_internal(
                entity_id=entity_id,
                mental_model=model,
                entity=entity,
                recent_memories=[fake_memory],
                vault_id=GLOBAL_VAULT_ID,
            )

    # The CRITICAL fix: CAS-abandon path returns None, NOT the model.
    assert returned is None, (
        'CAS abandon must return None so engine.reflect_batch filters '
        'this entity out of all_success_models and the service layer '
        'routes to mark_failed (re-enqueue), not complete_reflection '
        '(delete queue row).'
    )

    # And the row stays at the winner's version — our stale write did
    # not overwrite the concurrent refresh.
    async with session_manager() as observer_session:
        result = await observer_session.execute(
            select(MentalModel).where(MentalModel.id == model_id)
        )
        persisted = result.scalar_one()
        assert persisted.version == 4, (
            f'winner version=4 must survive; observed {persisted.version}'
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflect_batch_tracks_cas_abandons_separately_from_success(
    session_manager, memex_config: MemexConfig
):
    """Round-5 HIGH regression guard: ``ReflectionEngine.last_abandoned_entity_ids``
    captures CAS-abandoned entities so the service layer can route them
    through ``mark_abandoned`` (no retry_count increment) instead of
    ``mark_failed`` (counts toward DEAD_LETTER).

    A hot entity in a multi-worker cluster could race repeatedly. Without
    this separation, three unlucky CAS losses would DEAD_LETTER it.
    """
    from unittest.mock import patch, AsyncMock
    from memex_core.memory.sql_models import MemoryUnit
    from memex_common.types import FactTypes
    from memex_core.memory.reflect.models import ReflectionRequest
    from sqlalchemy import update as sa_update

    async with session_manager() as setup_session:
        entity, model = await _seed_entity_and_model(setup_session, version=2)
        entity_id = entity.id
        model_id = model.id

    # Concurrent winner bumps the row.
    async with session_manager() as winner_session:
        await winner_session.execute(
            sa_update(MentalModel).where(MentalModel.id == model_id).values(version=3)
        )
        await winner_session.commit()

    async with session_manager() as orchestrator_session:
        engine = ReflectionEngine(
            orchestrator_session,
            memex_config,
            _make_embedder(),
            entity_session_factory=session_manager,
        )
        engine.lm = MagicMock()
        with (
            patch.object(engine, '_phase_1_seed', new_callable=AsyncMock) as mock_seed,
            patch.object(engine, '_phase_2_hunt', new_callable=AsyncMock) as mock_hunt,
            patch.object(engine, '_phase_3_validate', new_callable=AsyncMock) as mock_val,
            patch.object(engine, '_phase_4_compare', new_callable=AsyncMock) as mock_comp,
        ):
            mock_seed.return_value = []
            mock_hunt.return_value = []
            mock_val.return_value = []
            mock_comp.return_value = (
                [Observation(title='Stale', content='will lose', evidence=[])],
                'summary',
            )

            # ``model`` in setup_session is at version=2; the winner bumped to 3.
            # The orchestrator's _batch_get_or_create_models will re-read,
            # so we patch that to return our stale-version copy.
            async def fake_get_or_create(entity_ids, vault_id=GLOBAL_VAULT_ID):
                return {entity_id: model}

            async def fake_get_entities(entity_ids):
                return {entity_id: entity}

            async def fake_fetch_memories(entity_ids, vault_id=None, limit_per_entity=20):
                return {
                    entity_id: [
                        MemoryUnit(
                            id=uuid4(),
                            text='trigger',
                            vault_id=GLOBAL_VAULT_ID,
                            fact_type=FactTypes.WORLD,
                            embedding=[0.0] * 384,
                        )
                    ]
                }

            engine._batch_get_or_create_models = fake_get_or_create  # type: ignore[assignment]
            engine._batch_get_entities = fake_get_entities  # type: ignore[assignment]
            engine._batch_fetch_recent_memories = fake_fetch_memories  # type: ignore[assignment]

            results = await engine.reflect_batch(
                [ReflectionRequest(entity_id=entity_id, vault_id=GLOBAL_VAULT_ID)]
            )

    # Engine returns empty applied list — abandoned entity went elsewhere.
    assert results == [], "CAS-abandoned entity must not appear in reflect_batch's applied list."
    # And the abandon was recorded for the service-layer router.
    assert engine.last_abandoned_entity_ids == [entity_id], (
        'CAS-abandoned entity_id must be tracked in last_abandoned_entity_ids '
        'so the service layer routes it through mark_abandoned (no retry++) '
        'instead of mark_failed.'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflect_entity_internal_prune_only_cas_abandon_returns_none(
    session_manager, memex_config: MemexConfig
):
    """Round-4 MEDIUM: the no-recent-memories prune-only path must also
    return None on CAS abandon (parallel of the main-path regression guard).
    """
    from unittest.mock import patch, AsyncMock
    from sqlalchemy import update as sa_update

    async with session_manager() as setup_session:
        entity, model = await _seed_entity_and_model(setup_session, version=7)
        entity_id = entity.id
        model_id = model.id

    # Concurrent winner bumps the row.
    async with session_manager() as winner_session:
        await winner_session.execute(
            sa_update(MentalModel).where(MentalModel.id == model_id).values(version=8)
        )
        await winner_session.commit()

    async with session_manager() as orchestrator_session:
        engine = ReflectionEngine(
            orchestrator_session,
            memex_config,
            _make_embedder(),
            entity_session_factory=session_manager,
        )
        engine.lm = MagicMock()
        # Force phase_0_mutated=True so the prune-only path enters Phase 5.
        with patch.object(engine, '_phase_0_update', new_callable=AsyncMock) as mock_p0:
            mock_p0.return_value = (
                [Observation(title='Pruned', content='ok', evidence=[])],
                True,
            )
            returned = await engine._reflect_entity_internal(
                entity_id=entity_id,
                mental_model=model,
                entity=entity,
                recent_memories=[],  # triggers prune-only branch
                vault_id=GLOBAL_VAULT_ID,
            )

    assert returned is None, (
        'prune-only CAS abandon must also return None so the queue layer re-enqueues for retry.'
    )
