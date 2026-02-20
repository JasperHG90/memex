import pytest
from uuid import uuid4
from datetime import datetime, timezone
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from memex_core.memory.sql_models import (
    MentalModel,
    Observation,
    EvidenceItem as ObservationEvidence,
)
from memex_core.memory.sql_models import Entity


@pytest.mark.asyncio
async def test_create_and_retrieve_mental_model(session: AsyncSession):
    """
    Verify we can save a MentalModel with nested Observations and retrieve it.
    """
    # 1. Create a parent Entity
    entity_id = uuid4()
    entity = Entity(id=entity_id, canonical_name='Test Entity')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    # 2. Create Observations
    obs_ev = ObservationEvidence(
        memory_id=uuid4(), quote='Something happened', timestamp=datetime.now(timezone.utc)
    )

    obs = Observation(title='Key Insight', content='The entity is growing.', evidence=[obs_ev])

    # 3. Create MentalModel
    mm = MentalModel(
        entity_id=entity.id,
        name='Test Entity Model',
        observations=[obs.model_dump(mode='json')],
    )
    session.add(mm)
    await session.commit()

    # 4. Clear session to force reload from DB
    session.expunge_all()

    # 5. Retrieve
    stmt = select(MentalModel).where(MentalModel.id == mm.id)
    result = await session.exec(stmt)
    retrieved_mm = result.one()

    # 6. Verify basics
    assert retrieved_mm.entity_id == entity.id
    assert retrieved_mm.name == 'Test Entity Model'
    assert retrieved_mm.version == 1

    # 7. Verify JSON serialization/deserialization
    # Note: SQLModel/SQLAlchemy usually returns the JSON structure (dicts), not Pydantic objects,
    # unless a custom type is used. We explicitly check this behavior.
    assert len(retrieved_mm.observations) == 1
    first_obs_data = retrieved_mm.observations[0]

    # Depending on SQLModel version/config, this might be a dict or an object.
    # We want to ensure we can work with it.
    if isinstance(first_obs_data, dict):
        # Manually validate back to Pydantic if it comes back as dict
        validated_obs = Observation(**first_obs_data)
    else:
        validated_obs = first_obs_data

    assert validated_obs.title == 'Key Insight'
    assert validated_obs.evidence[0].quote == 'Something happened'
    # Comparison should work as Observation(**data) converted string back to UUID
    assert validated_obs.evidence[0].memory_id == obs_ev.memory_id
