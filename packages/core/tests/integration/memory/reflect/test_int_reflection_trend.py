import pytest
import os
import dspy
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.reflect.models import ReflectionRequest
from memex_common.types import FactTypes
from memex_core.memory.sql_models import MemoryUnit, Entity, UnitEntity, Trend
from memex_core.memory.models.embedding import get_embedding_model


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_reflection_trend_stale_integration(session: AsyncSession, memex_config):
    """
    Integration test ensuring that reflection on OLD data produces STALE trends.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    # 1. Setup
    lm = dspy.LM('gemini/gemini-3-flash-preview', api_key=api_key)

    with dspy.context(lm=lm):
        embedder = await get_embedding_model()
        engine = ReflectionEngine(session, config=memex_config, embedder=embedder)
        engine.lm = lm

        # 2. Create Entity
        entity_id = uuid4()
        entity = Entity(
            id=entity_id,
            canonical_name='Ancient History',
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        session.add(entity)

        # 3. Create "Stale" Memories (100 days old)
        old_date = datetime.now(timezone.utc) - timedelta(days=100)

        memories_data = [
            'The Roman Empire fell in 476 AD.',
            'Julius Caesar was assassinated in 44 BC.',
        ]

        embeddings = embedder.encode(memories_data)

        for i, text in enumerate(memories_data):
            unit = MemoryUnit(
                id=uuid4(),
                text=text,
                embedding=embeddings[i].tolist(),
                event_date=old_date,  # KEY: Set event date to past
                fact_type=FactTypes.WORLD,
            )
            session.add(unit)

            # Link to Entity
            link = UnitEntity(unit_id=unit.id, entity_id=entity_id)
            session.add(link)

        await session.commit()

        # 4. Run Reflection
        request = ReflectionRequest(entity_id=entity_id)
        mental_model = await engine.reflect_on_entity(request)

        # 5. Verify Results
        assert mental_model is not None
        assert len(mental_model.observations) > 0

        # Check that at least one observation has a STALE trend
        stale_count = 0
        for obs in mental_model.observations:
            # obs is a dict here because it came from JSONB in DB or was serialized
            trend = obs.get('trend')
            print(f'DEBUG Integration: Obs Title: {obs.get("title")}, Trend: {trend}')

            # The logic in ReflectionEngine._phase_4_compare serializes the Enum to value
            if trend == Trend.STALE.value or trend == Trend.STALE:
                stale_count += 1

        assert stale_count > 0, (
            f'Expected at least one STALE observation, found {stale_count}. Observations: {mental_model.observations}'
        )
