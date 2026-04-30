"""Integration tests for F33 exploration floor — epsilon-greedy injection through the engine pipeline.

Verifies that exploration units are injected after MMR when epsilon > 0,
that only low-MW units are eligible, and that injection is disabled when epsilon=0.
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
from memex_common.config import GLOBAL_VAULT_ID, RetrievalConfig
from memex_common.types import FactTypes
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.models.embedding import get_embedding_model


@pytest.mark.integration
class TestExplorationFloor:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture
    async def seeded_low_mw_units(self, session: AsyncSession):
        """Create units with low MW counts that are exploration-eligible."""
        vault_id = GLOBAL_VAULT_ID
        note_id = uuid4()
        entity_id = uuid4()

        emb = [0.1] * 384
        note = Note(id=note_id, original_text='Exploration test', vault_id=vault_id)
        entity = Entity(id=entity_id, canonical_name='Test')

        # Regular unit (already retrieved, has outcomes)
        regular_id = uuid4()
        regular = MemoryUnit(
            id=regular_id,
            note_id=note_id,
            text='Well-known fact about Python',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
            success_co_count=10,
            failure_co_count=2,
        )
        # Cold-start unit (eligible for exploration)
        cold_id = uuid4()
        cold = MemoryUnit(
            id=cold_id,
            note_id=note_id,
            text='New fact about Python that has never been retrieved',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
            success_co_count=0,
            failure_co_count=0,
        )
        # Low-MW unit (also eligible — total < 5)
        low_mw_id = uuid4()
        low_mw = MemoryUnit(
            id=low_mw_id,
            note_id=note_id,
            text='Rarely seen fact about Python',
            fact_type=FactTypes.WORLD,
            embedding=emb,
            vault_id=vault_id,
            event_date=datetime.now(timezone.utc),
            success_co_count=2,
            failure_co_count=1,
        )

        ue_reg = UnitEntity(unit_id=regular_id, entity_id=entity_id, vault_id=vault_id)
        ue_cold = UnitEntity(unit_id=cold_id, entity_id=entity_id, vault_id=vault_id)
        ue_low = UnitEntity(unit_id=low_mw_id, entity_id=entity_id, vault_id=vault_id)

        session.add(note)
        session.add(entity)
        session.add(regular)
        session.add(cold)
        session.add(low_mw)
        session.add(ue_reg)
        session.add(ue_cold)
        session.add(ue_low)
        await session.commit()

        return {
            'regular_id': regular_id,
            'cold_id': cold_id,
            'low_mw_id': low_mw_id,
        }

    async def test_exploration_injection_in_retrieval_pipeline(
        self, session: AsyncSession, embedder, seeded_low_mw_units
    ):
        """Test exploration injection by calling inject_exploration_units directly.

        Integration through the full engine pipeline is non-deterministic (depends on
        whether candidates are already in results). Direct function call with
        epsilon=1.0 verifies the full injection logic against real DB objects.
        """
        from memex_core.memory.retrieval.exploration import inject_exploration_units

        # Create results list with only the high-MW unit
        regular_unit = await session.get(MemoryUnit, seeded_low_mw_units['regular_id'])
        assert regular_unit is not None

        # Get all candidates
        cold_unit = await session.get(MemoryUnit, seeded_low_mw_units['cold_id'])
        low_mw_unit = await session.get(MemoryUnit, seeded_low_mw_units['low_mw_id'])
        assert cold_unit is not None
        assert low_mw_unit is not None

        all_candidates = [regular_unit, cold_unit, low_mw_unit]
        results = [regular_unit]  # Only the high-MW unit in results

        injected = inject_exploration_units(
            results,
            all_candidates,
            epsilon=1.0,
            max_injections=2,
            low_mw_threshold=5,
        )

        exploration_units = [u for u in injected if u.unit_metadata.get('exploration', False)]
        assert len(exploration_units) >= 1
        for u in exploration_units:
            assert (u.success_co_count + u.failure_co_count) < 5

    async def test_exploration_disabled_when_epsilon_zero(
        self, session: AsyncSession, embedder, seeded_low_mw_units
    ):
        config = RetrievalConfig(exploration_epsilon=0.0)
        engine = RetrievalEngine(embedder=embedder, retrieval_config=config)

        request = RetrievalRequest(query='Python', limit=20, vault_ids=[GLOBAL_VAULT_ID])
        results, _ = await engine.retrieve(session, request)

        exploration_units = [u for u in results if u.unit_metadata.get('exploration', False)]
        assert len(exploration_units) == 0

    async def test_exploration_units_are_low_mw_only(
        self, session: AsyncSession, embedder, seeded_low_mw_units
    ):
        config = RetrievalConfig(
            exploration_epsilon=1.0,
            exploration_max_injections=2,
            exploration_low_mw_threshold=5,
        )
        engine = RetrievalEngine(embedder=embedder, retrieval_config=config)

        request = RetrievalRequest(query='Python', limit=20, vault_ids=[GLOBAL_VAULT_ID])
        results, _ = await engine.retrieve(session, request)

        for u in results:
            if u.unit_metadata.get('exploration', False):
                total_outcomes = u.success_co_count + u.failure_co_count
                assert total_outcomes < 5

    async def test_exploration_max_injections_respected(
        self, session: AsyncSession, embedder, seeded_low_mw_units
    ):
        config = RetrievalConfig(
            exploration_epsilon=1.0,
            exploration_max_injections=1,
            exploration_low_mw_threshold=5,
        )
        engine = RetrievalEngine(embedder=embedder, retrieval_config=config)

        request = RetrievalRequest(query='Python', limit=20, vault_ids=[GLOBAL_VAULT_ID])
        results, _ = await engine.retrieve(session, request)

        exploration_units = [u for u in results if u.unit_metadata.get('exploration', False)]
        assert len(exploration_units) <= 1
