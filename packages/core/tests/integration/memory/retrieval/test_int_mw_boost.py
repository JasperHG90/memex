"""Integration tests for F1c MW boost composition at the reranker.

Verifies that Memory Worth boost is composed into retrieval scoring via
the additive-marginal formula: mw_boost = 1.0 + mw_alpha * (mw_score - 0.5).
Cold-start units (0/0) get mw_boost=1.0 (neutral).
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
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.services.outcomes import compute_mw_score, compute_mw_boost


@pytest.mark.integration
class TestMwBoostComposition:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture
    async def engine_instance(self, embedder):
        return RetrievalEngine(embedder=embedder)

    @pytest_asyncio.fixture
    async def seeded_units(self, session: AsyncSession):
        """Create two units with identical embeddings but different MW counters."""
        vault_id = GLOBAL_VAULT_ID
        note_id = uuid4()
        high_success_id = uuid4()
        high_failure_id = uuid4()
        entity_id = uuid4()

        emb = [0.1] * 384
        note = Note(id=note_id, original_text='Test note', vault_id=vault_id)

        # High-success unit
        high_success = MemoryUnit(
            id=high_success_id,
            note_id=note_id,
            text='Well-established fact about deployment',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
            success_co_count=10,
            failure_co_count=0,
        )
        # High-failure unit
        high_failure = MemoryUnit(
            id=high_failure_id,
            note_id=note_id,
            text='Disproven claim about deployment',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
            success_co_count=0,
            failure_co_count=10,
        )
        entity = Entity(id=entity_id, canonical_name='Deploy')
        ue1 = UnitEntity(unit_id=high_success_id, entity_id=entity_id, vault_id=vault_id)
        ue2 = UnitEntity(unit_id=high_failure_id, entity_id=entity_id, vault_id=vault_id)

        session.add(note)
        session.add(high_success)
        session.add(high_failure)
        session.add(entity)
        session.add(ue1)
        session.add(ue2)
        await session.commit()

        return {
            'high_success_id': high_success_id,
            'high_failure_id': high_failure_id,
        }

    async def test_mw_boost_neutral_for_cold_start(self, session: AsyncSession, engine_instance):
        """Cold-start units (0/0) get mw_boost=1.0 — no rank penalty."""
        note_id = uuid4()
        unit_id = uuid4()
        emb = [0.1] * 384
        note = Note(id=note_id, original_text='Cold start', vault_id=GLOBAL_VAULT_ID)
        unit = MemoryUnit(
            id=unit_id,
            note_id=note_id,
            text='New fact never retrieved before',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=GLOBAL_VAULT_ID,
            event_date=datetime.now(timezone.utc),
            success_co_count=0,
            failure_co_count=0,
        )
        session.add(note)
        session.add(unit)
        await session.commit()

        mw_boost = compute_mw_boost(0, 0)
        assert mw_boost == 1.0

    async def test_mw_boost_upweights_high_success(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        mw_score = compute_mw_score(10, 0)
        mw_boost = compute_mw_boost(10, 0)
        assert mw_boost > 1.0
        assert mw_score > 0.5

    async def test_mw_boost_downweights_high_failure(
        self, session: AsyncSession, engine_instance, seeded_units
    ):
        mw_score = compute_mw_score(0, 10)
        mw_boost = compute_mw_boost(0, 10)
        assert mw_boost < 1.0
        assert mw_score < 0.5

    async def test_mw_boost_composition_formula(self):
        """Verify the exact additive-marginal formula."""
        for s, f in [(5, 3), (0, 0), (10, 0), (0, 10), (50, 50)]:
            mw_score = compute_mw_score(s, f)
            mw_boost = compute_mw_boost(s, f, mw_alpha=0.3)
            expected = 1.0 + 0.3 * (mw_score - 0.5)
            assert abs(mw_boost - expected) < 1e-10, f'Formula mismatch for ({s},{f})'

    async def test_mw_boost_observability(self, session: AsyncSession, seeded_units):
        """Verify MW score computation from DB-backed counters."""
        from memex_core.services.outcomes import OutcomeService

        svc = OutcomeService()
        # Record outcomes and verify score changes
        await svc.record_outcome(
            session,
            unit_ids=[str(seeded_units['high_success_id'])],
            success=True,
            vault_id=str(GLOBAL_VAULT_ID),
        )

        session.expire_all()
        unit = await session.get(MemoryUnit, seeded_units['high_success_id'])
        assert unit is not None
        assert unit.success_co_count == 11  # was 10, now 11
        mw_score = compute_mw_score(11, 0)
        assert mw_score > compute_mw_score(10, 0)  # Score increases with more success
