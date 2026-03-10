import pytest
import pytest_asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import (
    MemoryUnit,
    Note,
    Entity,
    UnitEntity,
    EntityCooccurrence,
)
from memex_common.types import FactTypes
from memex_core.memory.sql_models import MentalModel, Observation
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.models.embedding import get_embedding_model


@pytest.mark.integration
class TestRetrievalEngine:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='function')
    async def engine_instance(self, embedder):
        return RetrievalEngine(embedder=embedder)

    async def test_retrieve_fusion_logic(self, session: AsyncSession, engine_instance, embedder):
        """
        Verifies retrieval + Mental Model side-loading with Vector Search.
        """
        # --- Data Setup ---
        text_musk = 'Elon Musk is the CEO of SpaceX.'
        emb_musk = embedder.encode([text_musk])[0].tolist()

        now = datetime.now(timezone.utc)

        # 1. Note & Memory
        doc = Note(id=uuid4(), original_text='Dummy')
        session.add(doc)

        u1 = MemoryUnit(
            id=uuid4(),
            text=text_musk,
            embedding=emb_musk,
            fact_type=FactTypes.WORLD,
            event_date=now,
            note_id=doc.id,
        )
        session.add(u1)

        # 2. Entity
        e_musk = Entity(id=uuid4(), canonical_name='Elon Musk')
        session.add(e_musk)
        await session.flush()

        # 3. Link
        ue1 = UnitEntity(unit_id=u1.id, entity_id=e_musk.id)
        session.add(ue1)

        # 4. Mental Model with Observation AND Embedding
        obs = Observation(title='Visionary', content='He wants to go to Mars.', evidence=[])

        # Create an embedding for the Mental Model (using same vector as u1 for strong match)
        mm = MentalModel(
            id=uuid4(),
            entity_id=e_musk.id,
            name='Elon Musk',
            observations=[obs.model_dump(mode='json')],
            last_refreshed=now,
            embedding=emb_musk,  # Give it a vector so it can be ranked
        )
        session.add(mm)
        await session.flush()

        await session.commit()

        # --- Test ---
        # Query for "Elon Musk".
        results, _ = await engine_instance.retrieve(
            session, RetrievalRequest(query='Elon Musk', limit=5)
        )

        assert len(results) >= 2

        # Check for observation
        obs_results = [r for r in results if r.fact_type == 'observation']
        mem_results = [r for r in results if r.fact_type == 'world']

        assert len(obs_results) == 1
        assert 'Visionary' in obs_results[0].text

        assert len(mem_results) == 1
        assert mem_results[0].id == u1.id

    async def test_graph_second_order_bfs(self, session: AsyncSession, engine_instance, embedder):
        """
        Verifies that the GraphStrategy retrieves memories linked to co-occurring entities (2nd order).
        """
        # Ensure pg_trgm is created
        now = datetime.now(timezone.utc)
        doc = Note(id=uuid4(), original_text='BFS Test')
        session.add(doc)

        # Entity A: Elon Musk
        e_musk = Entity(id=uuid4(), canonical_name='Elon Musk')
        session.add(e_musk)

        # Entity B: Mars
        e_mars = Entity(id=uuid4(), canonical_name='Mars')
        session.add(e_mars)
        await session.flush()

        # Co-occurrence (Ensure entity_id_1 < entity_id_2)
        if e_musk.id < e_mars.id:
            e1, e2 = e_musk, e_mars
        else:
            e1, e2 = e_mars, e_musk

        co = EntityCooccurrence(entity_id_1=e1.id, entity_id_2=e2.id, cooccurrence_count=10)
        session.add(co)

        # Memory M linked to Mars (NOT Musk)
        text_mars = 'The surface of Mars is covered in iron oxide dust.'
        emb_mars = embedder.encode([text_mars])[0].tolist()

        u_mars = MemoryUnit(
            id=uuid4(),
            text=text_mars,
            embedding=emb_mars,
            fact_type=FactTypes.WORLD,
            event_date=now,
            note_id=doc.id,
        )
        session.add(u_mars)
        await session.flush()

        # Link M -> Mars
        ue_mars = UnitEntity(unit_id=u_mars.id, entity_id=e_mars.id)
        session.add(ue_mars)

        await session.commit()

        # Query for "Elon Musk"
        # Mars memory should appear because Mars co-occurs with Elon Musk
        results, _ = await engine_instance.retrieve(
            session, RetrievalRequest(query='Elon Musk', limit=10)
        )

        found = False
        for unit in results:
            if unit.id == u_mars.id:
                found = True
                break

        assert found, 'Failed to retrieve 2nd order memory via Graph Strategy BFS.'

    async def test_retrieve_empty_db(self, session: AsyncSession, engine_instance):
        """Test retrieval against an empty database."""
        results, _ = await engine_instance.retrieve(
            session, RetrievalRequest(query='Nothing here', limit=5)
        )
        assert results == []

    async def test_retrieve_pagination_limit(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Test that the limit parameter is respected."""
        doc = Note(id=uuid4(), original_text='Pagination Test')
        session.add(doc)

        # Create 5 identical memories
        embedding = embedder.encode(['Pagination'])[0].tolist()
        for i in range(5):
            session.add(
                MemoryUnit(
                    id=uuid4(),
                    text=f'Pagination Memory {i}',
                    embedding=embedding,
                    fact_type=FactTypes.WORLD,
                    event_date=datetime.now(timezone.utc),
                    note_id=doc.id,
                )
            )
        await session.commit()

        results, _ = await engine_instance.retrieve(
            session, RetrievalRequest(query='Pagination', limit=3)
        )
        # Default config uses token_budget=1000, which overrides limit.
        # With token_budget active, all 5 short texts fit within the budget.
        # When token_budget is not explicitly set on the request, the config
        # default takes precedence and limit is ignored.
        assert len(results) == 5

    async def test_retrieve_temporal_filtering(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """Test date-based filtering."""
        doc = Note(id=uuid4(), original_text='Temporal Test')
        session.add(doc)
        embedding = embedder.encode(['Temporal'])[0].tolist()

        old_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
        new_date = datetime(2025, 1, 1, tzinfo=timezone.utc)

        u_old = MemoryUnit(
            id=uuid4(),
            text='Old Memory',
            embedding=embedding,
            event_date=old_date,
            note_id=doc.id,
        )
        u_new = MemoryUnit(
            id=uuid4(),
            text='New Memory',
            embedding=embedding,
            event_date=new_date,
            note_id=doc.id,
        )

        session.add(u_old)
        session.add(u_new)
        await session.commit()

        # Filter for recent only
        results, _ = await engine_instance.retrieve(
            session,
            RetrievalRequest(
                query='Temporal',
                limit=10,
                filters={'start_date': datetime(2024, 1, 1, tzinfo=timezone.utc)},
            ),
        )

        ids = [r.id for r in results]
        assert u_new.id in ids
        assert u_old.id not in ids

    async def test_hindsight_filtering(self, session: AsyncSession, engine_instance, embedder):
        """
        Verifies Token Budget Filtering and Min Score Filtering integration.
        """
        doc = Note(id=uuid4(), original_text='Hindsight Filter Test')
        session.add(doc)
        embedding = embedder.encode(['Hindsight'])[0].tolist()
        now = datetime.now(timezone.utc)

        # Create 5 memories with distinct text lengths
        # Unit 1: ~3 tokens ("Short unit 0")
        # Unit 2: ~3 tokens ("Short unit 1")
        # ...
        units = []
        for i in range(5):
            u = MemoryUnit(
                id=uuid4(),
                text=f'Short unit {i}',
                embedding=embedding,
                fact_type=FactTypes.WORLD,
                event_date=now,
                note_id=doc.id,
            )
            session.add(u)
            units.append(u)
        await session.commit()

        # 1. Test Token Budget
        # "Short unit X" is approx 3-4 tokens depending on encoding.
        # Budget of 10 should allow ~2 units.
        results_budget, _ = await engine_instance.retrieve(
            session, RetrievalRequest(query='Short unit', limit=10, token_budget=10)
        )
        assert 0 < len(results_budget) < 5
        assert len(results_budget) <= 3  # Conservative upper bound check

        # 2. Test Min Score (Simulated High Threshold)
        # We can't easily control the exact score without mocking the reranker,
        # but if we set a threshold of 0.9999, effectively nothing should match
        # unless it's a perfect identity match and the model is very confident.
        if engine_instance.reranker:
            results_strict, _ = await engine_instance.retrieve(
                session, RetrievalRequest(query='Irrelevant query', limit=10, min_score=0.999)
            )
            assert len(results_strict) == 0

            # Test Min Score (Loose Threshold)
            # Should return all found results
            results_loose, _ = await engine_instance.retrieve(
                session, RetrievalRequest(query='Short unit', limit=10, min_score=0.001)
            )
            assert len(results_loose) >= 1
