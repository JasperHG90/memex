"""Race-safety gate for mental-model get-or-create.

``mental_models`` carries a *full* unique index on ``(entity_id, vault_id)`` —
one row per entity per vault (the singleton invariant documented in
``scheduler.py``). When two reflection workers claim the same entity in one
scheduler tick, both ``_batch_get_or_create_models`` calls SELECT "missing" and
race to INSERT. A blind INSERT makes the orchestrator commit raise
``UniqueViolationError``, aborting the whole batch; the scheduler then logs
"Task failed" and the claimed queue items only retry after stale-processing
recovery (~30 min). That is the production error observed against v1.0.0rc1.

The fix makes the create idempotent (``ON CONFLICT DO NOTHING`` + re-SELECT) so
concurrent callers converge on a single row with no exception. This test drives
many concurrent callers through the real ``ReflectionEngine`` method against real
Postgres and asserts: nobody raises, exactly one row exists, and every caller
resolves to that same row.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlmodel import col, select

from memex_core.config import MemexConfig
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import Entity, MentalModel, Vault

_CONCURRENCY = 12


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_get_or_create_models_is_race_safe(
    metastore, memex_config: MemexConfig
) -> None:
    async with metastore.session() as s:
        vault = Vault(id=uuid4(), name=f'mm-race-{uuid4().hex[:8]}')
        entity = Entity(id=uuid4(), canonical_name='MCP servers')
        s.add_all([vault, entity])
        await s.commit()
        vault_id, entity_id = vault.id, entity.id

    async def worker():
        # Each worker gets its own session/connection so the INSERTs genuinely
        # contend on the unique index — mirroring two scheduler workers in a tick.
        async with metastore.session() as s:
            engine = ReflectionEngine(session=s, config=memex_config, embedder=MagicMock())
            models = await engine._batch_get_or_create_models([entity_id], vault_id=vault_id)
            # reflect_batch commits the freshly-created rows (line ~372); mirror it.
            await s.commit()
            return models[entity_id].id

    # No return_exceptions: a UniqueViolationError in any worker (the unfixed
    # behaviour) propagates and fails the test.
    ids = await asyncio.gather(*[worker() for _ in range(_CONCURRENCY)])

    # Every racer converged on the one row that won the unique slot.
    assert len(set(ids)) == 1, f'workers resolved to different rows: {set(ids)}'

    async with metastore.session() as s:
        rows = (
            await s.exec(
                select(MentalModel)
                .where(col(MentalModel.entity_id) == entity_id)
                .where(col(MentalModel.vault_id) == vault_id)
            )
        ).all()
    assert len(rows) == 1, f'expected exactly one mental_model row, got {len(rows)}'
    assert rows[0].id == ids[0]
