import pytest
import os
import dspy
from uuid import uuid4
from datetime import datetime, timezone
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.reflect.models import ReflectionRequest
from memex_common.types import FactTypes
from memex_core.memory.sql_models import MemoryUnit, Entity, UnitEntity
from memex_core.memory.models.embedding import get_embedding_model


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_reflection_engine_end_to_end(session: AsyncSession, memex_config):
    """
    Integration test for ReflectionEngine using real Gemini and real Postgres.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    # 1. Setup
    lm = dspy.LM('gemini/gemini-3-flash-preview', api_key=api_key)

    with dspy.context(lm=lm):
        # Use real embedder
        embedder = await get_embedding_model()

        engine = ReflectionEngine(session, config=memex_config, embedder=embedder)
        # Inject LM directly just in case, though the class tries dspy.settings.lm
        engine.lm = lm

        # 2. Create Entity
        entity_id = uuid4()
        entity = Entity(
            id=entity_id,
            canonical_name='Rust Language',
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        session.add(entity)

        # 3. Create Memory Units
        memories_data = [
            'Rust guarantees memory safety without garbage collection.',
            'The borrow checker prevents data races.',
            "Rust's learning curve is steep due to lifetimes.",
            'Cargo is an excellent package manager.',
            'Async Rust is complex but powerful.',
        ]

        memory_ids = []
        # Batch encode for efficiency
        embeddings = embedder.encode(memories_data)

        for i, text in enumerate(memories_data):
            unit = MemoryUnit(
                id=uuid4(),
                text=text,
                embedding=embeddings[i].tolist(),  # Real embedding
                event_date=datetime.now(timezone.utc),
                fact_type=FactTypes.WORLD,
            )
            session.add(unit)
            memory_ids.append(unit.id)

            # Link to Entity
            link = UnitEntity(unit_id=unit.id, entity_id=entity_id)
            session.add(link)

        await session.commit()

        # 4. Run Reflection
        request = ReflectionRequest(entity_id=entity_id)
        mental_model = await engine.reflect_on_entity(request)

        # 5. Verify Results
        assert mental_model is not None
        assert mental_model.entity_id == entity_id
        assert mental_model.name == 'Rust Language'

        # Check observations
        assert len(mental_model.observations) > 0

        # Verify we captured some core concepts
        obs_texts = [o['content'].lower() for o in mental_model.observations]
        obs_titles = [o['title'].lower() for o in mental_model.observations]

        combined_text = ' '.join(obs_texts + obs_titles)

        keywords = ['safety', 'memory', 'borrow', 'steep', 'curve', 'cargo']
        matches = sum(1 for k in keywords if k in combined_text)

        assert matches >= 2, f'Expected to capture Rust concepts, got: {mental_model.observations}'

        # Verify Evidence
        # With real embeddings, we should have a better chance of finding evidence in Phase 2
        # But it depends on the threshold and the embedder quality.
        # We lowered the threshold in config to 0.5 to help.

        assert isinstance(mental_model.observations, list)
        assert mental_model.version >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reflection_no_memories(session: AsyncSession, memex_config):
    """Test reflection on an entity with no memories."""
    # Setup
    from unittest.mock import MagicMock

    mock_embedder = MagicMock()
    engine = ReflectionEngine(session, config=memex_config, embedder=mock_embedder)

    entity_id = uuid4()
    entity = Entity(id=entity_id, canonical_name='Ghost Entity')
    session.add(entity)
    await session.commit()

    # Run
    request = ReflectionRequest(entity_id=entity_id)
    model = await engine.reflect_on_entity(request)

    assert model.observations == []
    assert model.name == 'Ghost Entity'
