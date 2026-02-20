import pytest
import pytest_asyncio
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    MemoryUnit,
    Document,
    Entity,
    UnitEntity,
    EntityCooccurrence,
)
from memex_core.memory.sql_models import MentalModel, Observation
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.reranking import get_reranking_model


@pytest.mark.integration
class TestRetrievalReranking:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    @pytest_asyncio.fixture(scope='class')
    async def reranker(self):
        return await get_reranking_model()

    @pytest_asyncio.fixture(scope='function')
    async def engine_instance(self, embedder, reranker):
        return RetrievalEngine(embedder=embedder, reranker=reranker)

    async def _setup_scenario(self, session: AsyncSession, embedder, scenario_data):
        """
        Helper to populate DB with mixed Facts, Opinions, and Mental Models.
        scenario_data: list of dicts with keys:
            - type ('fact', 'opinion', 'observation')
            - text
            - is_target (bool, optional)
            - entities (list[str], optional): Canonical names of entities to link
            - event_date (datetime, optional): Specific date for the memory
        """
        doc = Document(id=uuid4(), original_text='Scenario Setup Doc')
        session.add(doc)

        # Cache entities to avoid duplicates in the session
        entity_cache: dict[str, Entity] = {}

        async def get_or_create_entity(name):
            if name in entity_cache:
                return entity_cache[name]

            # Check DB first
            existing = (
                await session.exec(select(Entity).where(Entity.canonical_name == name))
            ).first()
            if existing:
                entity_cache[name] = existing
                return existing

            new_ent = Entity(id=uuid4(), canonical_name=name)
            session.add(new_ent)
            await session.flush()
            entity_cache[name] = new_ent
            return new_ent

        targets = []

        for item in scenario_data:
            embedding = embedder.encode([item['text']])[0].tolist()

            # Default to now if no date provided
            evt_date = item.get('event_date', datetime.now(timezone.utc))

            unit = None
            if item['type'] == 'fact':
                unit = MemoryUnit(
                    id=uuid4(),
                    text=item['text'],
                    embedding=embedding,
                    fact_type=FactTypes.WORLD,
                    event_date=evt_date,
                    document_id=doc.id,
                )
                session.add(unit)

            elif item['type'] == 'opinion':
                unit = MemoryUnit(
                    id=uuid4(),
                    text=item['text'],
                    embedding=embedding,
                    fact_type=FactTypes.OPINION,
                    confidence_alpha=2.0,
                    confidence_beta=2.0,
                    event_date=evt_date,
                    document_id=doc.id,
                )
                session.add(unit)

            elif item['type'] == 'observation':
                # Link to the first entity in the list, or a generic one if none
                ent_names = item.get('entities', ['System'])
                main_entity = await get_or_create_entity(ent_names[0])

                obs = Observation(
                    title=f'Obs: {item["text"][:20]}...', content=item['text'], evidence=[]
                )
                mm = MentalModel(
                    id=uuid4(),
                    entity_id=main_entity.id,
                    name=f'{main_entity.canonical_name} Model',
                    observations=[obs.model_dump(mode='json')],
                    last_refreshed=evt_date,
                    embedding=embedding,
                )
                session.add(mm)

            # If we created a Unit (Fact/Opinion), link it to entities
            if unit and 'entities' in item:
                for name in item['entities']:
                    ent = await get_or_create_entity(name)
                    link = UnitEntity(unit_id=unit.id, entity_id=ent.id)
                    session.add(link)

            if item.get('is_target'):
                targets.append(item['text'])

        await session.commit()
        return targets

    async def test_scenario_1_incident_post_mortem(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """
        Scenario 1: "Incident Post-Mortem"
        Query: "What is the consensus on the root cause of the Project Chimera outage last Tuesday?"
        """
        query = (
            'What is the consensus on the root cause of the Project Chimera outage last Tuesday?'
        )

        data = [
            # GOLD STANDARD
            {
                'type': 'observation',
                'text': 'Post-mortem analysis indicates the Project Chimera outage was primarily caused by a redis cache stampede triggering a cascade failure.',
                'is_target': True,
                'entities': ['Project Chimera', 'Redis'],
            },
            # Distractor 1 (Keyword Repeat)
            {
                'type': 'fact',
                'text': 'Project Chimera had a major outage on Tuesday.',
                'entities': ['Project Chimera'],
            },
            # Distractor 2 (Wrong Entity)
            {
                'type': 'observation',
                'text': 'The Project Pegasus outage was caused by a similar redis issue last year.',
                'entities': ['Project Pegasus', 'Redis'],
            },
            # Distractor 3 (General Context)
            {
                'type': 'fact',
                'text': 'Project Chimera uses Redis for caching and session storage.',
                'entities': ['Project Chimera', 'Redis'],
            },
        ]

        await self._setup_scenario(session, embedder, data)

        # Retrieve
        results = await engine_instance.retrieve(session, RetrievalRequest(query=query, limit=3))

        # Check Top 1
        assert len(results) >= 1
        top_result = results[0]
        # The gold standard observation should be ranked first because it explains the "root cause"
        assert 'cache stampede' in top_result.text
        assert 'Project Chimera' in top_result.text

    async def test_scenario_2_strategic_evolution(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """
        Scenario 2: "Strategic Evolution" (Temporal)
        Query: "How has our architectural stance on microservices evolved over the last year?"
        """
        query = 'How has our architectural stance on microservices evolved over the last year?'

        now = datetime.now(timezone.utc)
        one_year_ago = now - timedelta(days=365)

        data = [
            # GOLD STANDARD (Recent)
            {
                'type': 'opinion',
                'text': 'Over the last year, we are shifting away from fine-grained microservices towards a modular monolith to reduce operational complexity.',
                'is_target': True,
                'event_date': now,
            },
            # Distractor 1 (Old Stance - 1 year ago)
            {
                'type': 'opinion',
                'text': 'Microservices are the future of our architecture and will allow us to scale infinitely.',
                'event_date': one_year_ago,
            },
            # Distractor 2 (Implementation Detail)
            {
                'type': 'fact',
                'text': 'The user-service is implemented as a microservice using gRPC.',
                'event_date': now,
            },
            # Distractor 3 (Irrelevant)
            {
                'type': 'fact',
                'text': 'The architecture team meets every Friday.',
                'event_date': now,
            },
        ]

        await self._setup_scenario(session, embedder, data)

        results = await engine_instance.retrieve(session, RetrievalRequest(query=query, limit=3))

        assert len(results) >= 1
        top_result = results[0]
        # Should pick the "shifting away" / "modular monolith" text.
        assert 'modular monolith' in top_result.text

    async def test_scenario_3_customer_feedback(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """
        Scenario 3: "Customer Feedback Analysis"
        Query: "What are enterprise customers saying about the new dashboard UI performance?"
        """
        query = 'What are enterprise customers saying about the new dashboard UI performance?'

        data = [
            # GOLD STANDARD
            {
                'type': 'observation',
                'text': 'Enterprise clients have complained that the new dashboard UI is sluggish when loading large datasets, taking up to 5 seconds.',
                'is_target': True,
                'entities': ['Enterprise Customers', 'Dashboard UI'],
            },
            # Distractor 1 (Internal Opinion)
            {
                'type': 'opinion',
                'text': 'I think the new dashboard UI looks great and is very modern.',
                'entities': ['Dashboard UI'],
            },
            # Distractor 2 (Wrong Feature - Reporting API)
            {
                'type': 'observation',
                'text': 'Customers love the high performance of the new reporting API.',
                'entities': ['Reporting API', 'Customers'],
            },
            # Distractor 3 (Release Fact)
            {
                'type': 'fact',
                'text': 'The dashboard UI was updated in the Q3 release to use React.',
                'entities': ['Dashboard UI'],
            },
        ]

        await self._setup_scenario(session, embedder, data)

        results = await engine_instance.retrieve(session, RetrievalRequest(query=query, limit=3))

        assert len(results) >= 1
        top_result = results[0]
        # Should pick the complaint about "sluggish" / "5 seconds"
        assert 'sluggish' in top_result.text

    async def test_scenario_4_graph_retrieval(
        self, session: AsyncSession, engine_instance, embedder
    ):
        """
        Scenario 4: "Graph Strategy"
        Verifies that items are retrieved via Entity Co-occurrence (Graph Strategy) and then ranked.
        """
        query = 'What is the status of Project X?'

        # 1. Setup Data with Entity Linking
        data = [
            {
                'type': 'fact',
                'text': 'Project Y has been delayed due to funding issues.',
                'entities': ['Project Y'],  # Linked to Y
                'is_target': True,
            },
            {
                'type': 'fact',
                'text': 'I had a sandwich for lunch.',
                'entities': [],
                'is_target': False,
            },
        ]

        await self._setup_scenario(session, embedder, data)

        # 2. Manually Create Co-occurrence (X <-> Y)
        async def get_ent(name):
            return (await session.exec(select(Entity).where(Entity.canonical_name == name))).first()

        ent_y = await get_ent('Project Y')

        ent_x = Entity(id=uuid4(), canonical_name='Project X')
        session.add(ent_x)
        await session.flush()

        # Ensure order
        if ent_x.id < ent_y.id:
            e1, e2 = ent_x, ent_y
        else:
            e1, e2 = ent_y, ent_x

        co = EntityCooccurrence(entity_id_1=e1.id, entity_id_2=e2.id, cooccurrence_count=100)
        session.add(co)
        await session.commit()

        # Retrieve
        # Limit=5 to ensure we get both if available, but RRF should rank Y high.
        results = await engine_instance.retrieve(session, RetrievalRequest(query=query, limit=5))

        found = any('Project Y' in r.text for r in results)
        assert found, 'Graph strategy failed to retrieve co-occurring entity memory'
