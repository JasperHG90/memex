"""Integration tests for F1b deprioritization filter — is_deprioritized exclusion in retrieval.

Verifies that deprioritized memory units are excluded from retrieval results by default
and included when include_deprioritized=True.
"""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import (
    Entity,
    MemoryUnit,
    Note,
    UnitEntity,
)
from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.models.embedding import get_embedding_model


@pytest.mark.integration
class TestDeprioritizationFilter:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture
    async def engine_instance(self, embedder):
        return RetrievalEngine(embedder=embedder)

    @pytest_asyncio.fixture
    async def seeded_units(self, session: AsyncSession):
        """Create an active unit and a deprioritized unit with similar embeddings."""
        vault_id = GLOBAL_VAULT_ID
        note_id = uuid4()
        active_id = uuid4()
        deprioritized_id = uuid4()
        entity_id = uuid4()

        note = Note(id=note_id, original_text='Test note', vault_id=vault_id)
        # Active unit
        emb = [0.1] * 384
        active_unit = MemoryUnit(
            id=active_id,
            note_id=note_id,
            text='Active fact about testing',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
            is_deprioritized=False,
        )
        # Deprioritized unit
        deprioritized_unit = MemoryUnit(
            id=deprioritized_id,
            note_id=note_id,
            text='Deprioritized fact about testing',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
            is_deprioritized=True,
        )
        entity = Entity(id=entity_id, canonical_name='Test')
        ue_active = UnitEntity(unit_id=active_id, entity_id=entity_id, vault_id=vault_id)
        ue_dep = UnitEntity(unit_id=deprioritized_id, entity_id=entity_id, vault_id=vault_id)

        session.add(note)
        session.add(active_unit)
        session.add(deprioritized_unit)
        session.add(entity)
        session.add(ue_active)
        session.add(ue_dep)
        await session.commit()

        return {
            'active_id': active_id,
            'deprioritized_id': deprioritized_id,
        }

    async def test_deprioritized_units_excluded_by_default(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(query='testing', limit=10, vault_ids=[GLOBAL_VAULT_ID])
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = [r.id for r in results]
        assert seeded_units['active_id'] in result_ids
        assert seeded_units['deprioritized_id'] not in result_ids

    async def test_include_deprioritized_returns_deprioritized_units(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(
            query='testing', limit=10, include_deprioritized=True, vault_ids=[GLOBAL_VAULT_ID]
        )
        results, _ = await engine_instance.retrieve(session, request)

        result_ids = [r.id for r in results]
        assert seeded_units['active_id'] in result_ids
        assert seeded_units['deprioritized_id'] in result_ids

    async def test_deprioritized_flag_in_result_metadata(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(
            query='testing', limit=10, include_deprioritized=True, vault_ids=[GLOBAL_VAULT_ID]
        )
        results, _ = await engine_instance.retrieve(session, request)

        for unit in results:
            if unit.id == seeded_units['deprioritized_id']:
                assert unit.is_deprioritized is True
                break
        else:
            pytest.fail('Deprioritized unit not found in results')

    async def test_mixed_active_and_deprioritized(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        request = RetrievalRequest(query='testing', limit=10, vault_ids=[GLOBAL_VAULT_ID])
        results, _ = await engine_instance.retrieve(session, request)

        for unit in results:
            assert unit.is_deprioritized is False


@pytest.mark.integration
class TestArchiveCascadeRoundTrip:
    """Note-level archive cascades to per-unit deprioritization, the
    units stay reachable via ``include_deprioritized=True``, and a
    later ``set_note_status('active')`` restores them to default
    retrieval. End-to-end coverage of the archive AC."""

    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture
    async def engine_instance(self, embedder):
        return RetrievalEngine(embedder=embedder)

    @pytest_asyncio.fixture
    async def seeded_note_units(self, session: AsyncSession):
        vault_id = GLOBAL_VAULT_ID
        note_id = uuid4()
        unit_id = uuid4()
        entity_id = uuid4()
        emb = [0.1] * 384

        note = Note(
            id=note_id,
            original_text='Legacy warehouse note',
            vault_id=vault_id,
            content_hash=f'h-{uuid4().hex[:8]}',
        )
        unit = MemoryUnit(
            id=unit_id,
            note_id=note_id,
            text='Legacy warehouse runs on Redshift',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
            is_deprioritized=False,
        )
        entity = Entity(id=entity_id, canonical_name='Legacy warehouse')
        ue = UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=vault_id)
        session.add(note)
        session.add(unit)
        session.add(entity)
        session.add(ue)
        await session.commit()
        return {'note_id': note_id, 'unit_id': unit_id}

    async def test_archive_suppresses_and_restore_revives(
        self, session: AsyncSession, engine_instance, seeded_note_units, api
    ) -> None:
        note_id = seeded_note_units['note_id']
        unit_id = seeded_note_units['unit_id']

        baseline_request = RetrievalRequest(
            query='legacy warehouse', limit=10, vault_ids=[GLOBAL_VAULT_ID]
        )
        baseline_results, _ = await engine_instance.retrieve(session, baseline_request)
        assert unit_id in [r.id for r in baseline_results]

        await api.set_note_status(note_id, 'archived')
        # Service used its own session; expire the test session's identity
        # map so retrieval sees the post-commit unit state.
        session.expire_all()

        default_results, _ = await engine_instance.retrieve(session, baseline_request)
        assert unit_id not in [r.id for r in default_results]

        include_request = RetrievalRequest(
            query='legacy warehouse',
            limit=10,
            include_deprioritized=True,
            vault_ids=[GLOBAL_VAULT_ID],
        )
        include_results, _ = await engine_instance.retrieve(session, include_request)
        include_ids = [r.id for r in include_results]
        assert unit_id in include_ids
        archived_unit = next(r for r in include_results if r.id == unit_id)
        assert archived_unit.is_deprioritized is True

        await api.set_note_status(note_id, 'active')
        session.expire_all()

        restored_results, _ = await engine_instance.retrieve(session, baseline_request)
        assert unit_id in [r.id for r in restored_results]
        restored_unit = next(r for r in restored_results if r.id == unit_id)
        assert restored_unit.is_deprioritized is False
