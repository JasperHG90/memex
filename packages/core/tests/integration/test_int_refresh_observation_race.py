"""Integration test for the Phase A vs Phase C race in ``_refresh_observation``.

This test pins the deprio-leak fix that motivates PR #148 against a real
Postgres: when a concurrent ``_flip_deprioritized`` commits between Phase A
(read tx that snapshots live evidence) and Phase C (CAS write tx), the
worker MUST raise ``RefreshStaleReadError`` and MUST NOT advance the
``mental_models.version`` — the scheduler reclaims the row with a backoff
and re-runs Phase A on current state on the next tick.

The unit-level CAS-abandon path is covered by the in-memory tests; this
test specifically exercises the live-evidence-set re-validation introduced
in the round-12 patch, which only fires against real DB MVCC.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.reflect.exceptions import (
    AdvisoryLockTakenError,
    RefreshStaleReadError,
)
from memex_core.memory.reflect.prompts import RefreshedObservation
from memex_core.memory.reflect.reflection import get_reflection_engine
from memex_core.memory.sql_models import (
    Entity,
    MemoryUnit,
    MentalModel,
    ReflectionQueue,
    ReflectionStatus,
    Vault,
)

pytestmark = [pytest.mark.integration]


@pytest.mark.asyncio
async def test_concurrent_deprio_between_phase_a_and_c_triggers_stale_read_reclaim(
    metastore,
    memex_config,
    mock_embedding_model,
    mock_reranking_model,
    mock_ner_model,
    filestore,
    monkeypatch,
):
    """A deprio committed between Phase A and Phase C MUST raise ``RefreshStaleReadError``.

    Seeds 1 Entity, 1 Vault, 2 MemoryUnits, 1 MentalModel whose single
    Observation cites both MUs. Enqueues a PROCESSING refresh_observation
    row. Patches ``_invoke_refresh_signature_with_context`` to, *between
    Phase A and Phase C*, flip ``MU2.is_deprioritized=True`` in a separate
    session and commit. The CAS-write tx's live-evidence re-validation
    should then notice the divergence and raise.
    """
    vault_id = uuid4()
    entity_id = uuid4()
    mu1_id = uuid4()
    mu2_id = uuid4()
    obs_id = uuid4()

    # ---------- Seed ----------
    async with metastore.session() as session:
        session.add(Vault(id=vault_id, name=f'refresh-race-{vault_id.hex[:8]}'))
        session.add(Entity(id=entity_id, canonical_name=f'race-entity-{entity_id.hex[:8]}'))
        await session.flush()

        for mu_id in (mu1_id, mu2_id):
            session.add(
                MemoryUnit(
                    id=mu_id,
                    vault_id=vault_id,
                    text=f'fact citing entity {uuid4()}',
                    fact_type=FactTypes.WORLD,
                    embedding=[0.1] * 384,
                    event_date=datetime.now(timezone.utc),
                    is_deprioritized=False,
                )
            )

        mm = MentalModel(
            id=uuid4(),
            entity_id=entity_id,
            vault_id=vault_id,
            name='race-entity',
            version=7,
            observations=[
                {
                    'id': str(obs_id),
                    'title': 'Cites both MUs',
                    'content': 'Original content backed by MU1 and MU2',
                    'trend': 'new',
                    'evidence': [
                        {'memory_id': str(mu1_id), 'quote': None, 'relevance': 1.0},
                        {'memory_id': str(mu2_id), 'quote': None, 'relevance': 1.0},
                    ],
                }
            ],
            embedding=[0.1] * 384,
            last_refreshed=datetime.now(timezone.utc),
        )
        session.add(mm)

        queue_row = ReflectionQueue(
            id=uuid4(),
            entity_id=entity_id,
            vault_id=vault_id,
            task_type='refresh_observation',
            observation_id=obs_id,
            source_unit_id=mu1_id,
            status=ReflectionStatus.PROCESSING,
            priority_lane=True,
            priority_score=1.0,
        )
        session.add(queue_row)
        await session.commit()
        mm_id = mm.id
        original_version = mm.version

    # ---------- Build engine ----------
    engine = get_reflection_engine(
        session=None,  # type: ignore[arg-type]
        config=memex_config,
        embedder=mock_embedding_model,
        entity_session_factory=metastore.session,
    )

    # ---------- Patch Phase B ----------
    # The patched coroutine simulates the concurrent deprio: it commits
    # ``MU2.is_deprioritized=True`` in a separate session (Phase B has no
    # DB session open in production, so opening one here is safe) AND
    # returns a well-formed RefreshedObservation so the worker proceeds
    # into Phase C, where the re-validation MUST catch the divergence.
    async def fake_invoke(self, obs_context, surviving_context):
        async with metastore.session() as concurrent_session:
            session: AsyncSession = concurrent_session
            mu = await session.get(MemoryUnit, mu2_id)
            assert mu is not None, 'MU2 was not seeded'
            mu.is_deprioritized = True
            session.add(mu)
            await session.commit()
        # Brief yield so the commit is fully visible to the next tx.
        await asyncio.sleep(0)
        return RefreshedObservation(
            content='Restated on surviving evidence',
            title='Cites both MUs',
            should_drop=False,
        )

    monkeypatch.setattr(
        'memex_core.memory.reflect.reflection.ReflectionEngine._invoke_refresh_signature_with_context',
        fake_invoke,
    )

    # ---------- Reload queue row (Phase A reads from DB) ----------
    async with metastore.session() as session:
        item = (
            await session.exec(select(ReflectionQueue).where(ReflectionQueue.id == queue_row.id))
        ).one()

    # ---------- Act + Assert ----------
    with pytest.raises(RefreshStaleReadError) as excinfo:
        await engine._refresh_observation(item)

    # RefreshStaleReadError must subclass AdvisoryLockTakenError so the
    # scheduler's existing "reclaim without retry_count++" branch fires.
    assert isinstance(excinfo.value, AdvisoryLockTakenError)

    # ---------- Verify MentalModel.version unchanged ----------
    async with metastore.session() as session:
        persisted = (await session.exec(select(MentalModel).where(MentalModel.id == mm_id))).one()
        assert persisted.version == original_version, (
            f'expected version unchanged at {original_version}, got {persisted.version} '
            '— the CAS write must NOT have committed when Phase A/C diverged'
        )
        # The observation must still cite both MUs in evidence (Phase C aborted
        # before rewriting it).
        assert len(persisted.observations) == 1
        evidence = persisted.observations[0].get('evidence') or []
        assert {str(e['memory_id']) for e in evidence} == {str(mu1_id), str(mu2_id)}
