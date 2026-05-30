"""Integration tests for the reflection_queue partial-UNIQUE indices.

These tests pin two production invariants against a real Postgres:

1. ``ON CONFLICT`` arbiter inference for ``enqueue_priority_reflect``
   succeeds — the SQLAlchemy column-expression ``index_where`` must
   render to the same canonical form as the partial UNIQUE DDL.
2. The ``(entity, vault, observation_id)`` partial UNIQUE for refresh
   tasks dedupes across the ``pending`` → ``processing`` boundary so
   a concurrent re-enqueue cannot insert a duplicate while the first
   task is mid-flight.

Both rely on Postgres's text-normalisation of partial-index predicates;
a unit test against SQLite would not catch arbiter-inference mismatches.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlmodel import col, select

from memex_core.memory.reflect.queue_service import ReflectionQueue, ReflectionStatus
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.memory.sql_models import Entity, Vault

pytestmark = [pytest.mark.integration]


async def _seed_entity_and_vault(session) -> tuple[uuid.UUID, uuid.UUID]:
    vault_id = uuid.uuid4()
    entity_id = uuid.uuid4()
    session.add(Vault(id=vault_id, name=f'q-pu-{vault_id.hex[:8]}'))
    session.add(Entity(id=entity_id, canonical_name=f'arbiter-{entity_id.hex[:8]}'))
    await session.commit()
    return entity_id, vault_id


@pytest.mark.asyncio
async def test_priority_reflect_upsert_resolves_partial_unique_arbiter(
    metastore, memex_config
) -> None:
    """``enqueue_priority_reflect`` succeeds end-to-end (arbiter matches DDL).

    If SQLAlchemy renders ``ReflectionStatus.PENDING`` differently than the
    DDL string ``'pending'``, the upsert fails with ``CardinalityViolation``
    or ``InvalidColumnReference``. This test catches such drift before deploy.
    """
    qs = ReflectionQueueService(config=memex_config.server.memory.reflection)

    async with metastore.session() as session:
        entity_id, vault_id = await _seed_entity_and_vault(session)

    async with metastore.session() as session:
        first = await qs.enqueue_priority_reflect(session, {entity_id}, vault_id)
        assert first == 1
        # enqueue_priority_reflect leaves the commit to the caller; metastore
        # sessions roll back on exit, so commit or the rows never persist.
        await session.commit()

    async with metastore.session() as session:
        # Re-enqueue same entity: ON CONFLICT path; partial UNIQUE arbiter
        # must resolve, and the row must become priority_lane=True.
        second = await qs.enqueue_priority_reflect(session, {entity_id}, vault_id)
        assert second == 1
        await session.commit()

    async with metastore.session() as session:
        rows = (
            await session.exec(
                select(ReflectionQueue)
                .where(col(ReflectionQueue.entity_id) == entity_id)
                .where(col(ReflectionQueue.vault_id) == vault_id)
            )
        ).all()
        active = [r for r in rows if r.status == ReflectionStatus.PENDING]
        assert len(active) == 1, f'expected 1 PENDING reflect row, got {len(active)}: {rows}'
        assert active[0].priority_lane is True
        assert active[0].task_type == 'reflect'


@pytest.mark.asyncio
async def test_partial_unique_dedupes_concurrent_enqueue_during_processing(metastore) -> None:
    """Refresh-task partial UNIQUE covers both ``pending`` AND ``processing``.

    Reproduces the boundary case: task moves to PROCESSING during claim;
    a concurrent ``flush_deferred_observation_refresh`` for the same
    ``(entity, vault, observation_id)`` must dedupe rather than insert
    a duplicate.
    """
    observation_id = uuid.uuid4()

    async with metastore.session() as session:
        entity_id, vault_id = await _seed_entity_and_vault(session)
        row = ReflectionQueue(
            entity_id=entity_id,
            vault_id=vault_id,
            task_type='refresh_observation',
            observation_id=observation_id,
            status=ReflectionStatus.PROCESSING,
            priority_lane=True,
            priority_score=1.0,
        )
        session.add(row)
        await session.commit()

    async with metastore.session() as session:
        await session.execute(
            text(
                """
                INSERT INTO reflection_queue (
                    id, entity_id, vault_id, task_type, observation_id,
                    status, priority_lane, priority_score, retry_count,
                    max_retries, last_queued_at
                ) VALUES (
                    :id, :eid, :vid, 'refresh_observation', :obs,
                    'pending', TRUE, 1.0, 0,
                    3, now()
                )
                ON CONFLICT (entity_id, vault_id, observation_id)
                WHERE task_type = 'refresh_observation'
                  AND status IN ('pending', 'processing')
                DO NOTHING
                """
            ),
            {
                'id': uuid.uuid4(),
                'eid': entity_id,
                'vid': vault_id,
                'obs': observation_id,
            },
        )
        await session.commit()

    async with metastore.session() as session:
        rows = (
            await session.exec(
                select(ReflectionQueue)
                .where(col(ReflectionQueue.entity_id) == entity_id)
                .where(col(ReflectionQueue.vault_id) == vault_id)
                .where(col(ReflectionQueue.task_type) == 'refresh_observation')
            )
        ).all()
        assert len(rows) == 1, f'expected 1 row after dedupe, got {len(rows)}: {rows}'
        assert rows[0].status == ReflectionStatus.PROCESSING
