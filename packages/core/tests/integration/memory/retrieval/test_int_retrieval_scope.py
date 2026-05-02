"""F1b retrieval scope matrix — 4 combinations of include_deprioritized × include_stale.

Per cognitive-memory-research-report.md §3.1, the two scope flags are
orthogonal and compose independently in the retrieval WHERE clause:

| include_deprioritized | include_stale | effective WHERE                                |
| --------------------- | ------------- | ---------------------------------------------- |
| False (default)       | False         | status = ACTIVE  AND  is_deprioritized = false |
| True                  | False         | status = ACTIVE                                |
| False                 | True          | status IN (ACTIVE, STALE)  AND  is_dep = false |
| True                  | True          | status IN (ACTIVE, STALE)                      |

These tests seed one unit in each cell of the 2×2 status × deprioritized
matrix and exercise all four flag combinations to confirm the
``apply_generic_filters`` branch (``strategies.py:78``) composes correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.sql_models import (
    ContentStatus,
    Entity,
    MemoryUnit,
    Note,
    UnitEntity,
)


@pytest.mark.integration
class TestRetrievalScopeMatrix:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture
    async def engine_instance(self, embedder):
        return RetrievalEngine(embedder=embedder)

    @pytest_asyncio.fixture
    async def seeded_matrix(self, session: AsyncSession) -> dict[str, UUID]:
        """Seed one unit in each cell of (status × is_deprioritized)."""
        vault_id = GLOBAL_VAULT_ID
        note_id = uuid4()
        entity_id = uuid4()
        emb = [0.1] * 384

        note = Note(id=note_id, original_text='F1b scope matrix', vault_id=vault_id)
        entity = Entity(id=entity_id, canonical_name='Scope')
        session.add(note)
        session.add(entity)
        await session.flush()

        ids: dict[str, UUID] = {}
        for status, is_dep, key in [
            (ContentStatus.ACTIVE, False, 'active_normal'),
            (ContentStatus.ACTIVE, True, 'active_deprioritized'),
            (ContentStatus.STALE, False, 'stale_normal'),
            (ContentStatus.STALE, True, 'stale_deprioritized'),
        ]:
            uid = uuid4()
            ids[key] = uid
            session.add(
                MemoryUnit(
                    id=uid,
                    note_id=note_id,
                    text=f'Scope test unit {key}',
                    fact_type=FactTypes.WORLD,
                    embedding=emb,
                    vault_id=vault_id,
                    event_date=datetime.now(timezone.utc),
                    status=status,
                    is_deprioritized=is_dep,
                )
            )
            session.add(UnitEntity(unit_id=uid, entity_id=entity_id, vault_id=vault_id))

        await session.commit()
        return ids

    async def test_default_scope_excludes_stale_and_deprioritized(
        self, session: AsyncSession, engine_instance, seeded_matrix
    ):
        """Default: only ACTIVE & non-deprioritized."""
        request = RetrievalRequest(query='scope test', limit=10, vault_ids=[GLOBAL_VAULT_ID])
        results, _ = await engine_instance.retrieve(session, request)
        ids = {r.id for r in results}
        assert seeded_matrix['active_normal'] in ids
        assert seeded_matrix['active_deprioritized'] not in ids
        assert seeded_matrix['stale_normal'] not in ids
        assert seeded_matrix['stale_deprioritized'] not in ids

    async def test_include_deprioritized_only_adds_active_dep(
        self, session: AsyncSession, engine_instance, seeded_matrix
    ):
        """include_deprioritized=True, include_stale=False: ACTIVE (both)."""
        request = RetrievalRequest(
            query='scope test',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            include_deprioritized=True,
        )
        results, _ = await engine_instance.retrieve(session, request)
        ids = {r.id for r in results}
        assert seeded_matrix['active_normal'] in ids
        assert seeded_matrix['active_deprioritized'] in ids
        assert seeded_matrix['stale_normal'] not in ids
        assert seeded_matrix['stale_deprioritized'] not in ids

    async def test_include_stale_only_adds_active_and_stale_normal(
        self, session: AsyncSession, engine_instance, seeded_matrix
    ):
        """include_stale=True, include_deprioritized=False: non-dep (both statuses)."""
        request = RetrievalRequest(
            query='scope test',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            include_stale=True,
        )
        results, _ = await engine_instance.retrieve(session, request)
        ids = {r.id for r in results}
        assert seeded_matrix['active_normal'] in ids
        assert seeded_matrix['stale_normal'] in ids
        assert seeded_matrix['active_deprioritized'] not in ids
        assert seeded_matrix['stale_deprioritized'] not in ids

    async def test_both_flags_returns_all_four(
        self, session: AsyncSession, engine_instance, seeded_matrix
    ):
        """include_stale=True, include_deprioritized=True: all 4 cells."""
        request = RetrievalRequest(
            query='scope test',
            limit=10,
            vault_ids=[GLOBAL_VAULT_ID],
            include_stale=True,
            include_deprioritized=True,
        )
        results, _ = await engine_instance.retrieve(session, request)
        ids = {r.id for r in results}
        for key in (
            'active_normal',
            'active_deprioritized',
            'stale_normal',
            'stale_deprioritized',
        ):
            assert seeded_matrix[key] in ids, (
                f'unit {key} should appear when both scope flags are True; got ids={ids}'
            )
