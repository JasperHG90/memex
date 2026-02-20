import pytest
from uuid import uuid4
from datetime import datetime, timezone
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.sql_models import Entity, ReflectionQueue, ReflectionStatus


@pytest.mark.asyncio
@pytest.mark.integration
async def test_persist_entity_resonance(session: AsyncSession):
    """Test saving and retrieving an entity with resonance metrics."""
    entity_id = uuid4()
    now = datetime.now(timezone.utc)

    entity = Entity(
        id=entity_id,
        canonical_name=f'Resonance Entity {uuid4()}',
        retrieval_count=42,
        last_retrieved_at=now,
    )
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    # Retrieve
    stmt = select(Entity).where(Entity.id == entity_id)
    result = await session.exec(stmt)
    retrieved_entity = result.one()

    assert retrieved_entity.retrieval_count == 42
    assert retrieved_entity.last_retrieved_at is not None
    assert abs((retrieved_entity.last_retrieved_at - now).total_seconds()) < 1.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_persist_reflection_queue_priority(session: AsyncSession):
    """Test saving and retrieving a ReflectionQueue item with float priority."""
    # Create Entity first (foreign key constraint)
    entity = Entity(canonical_name=f'Priority Entity {uuid4()}')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    # Create Queue Item
    queue_item = ReflectionQueue(
        entity_id=entity.id,
        priority_score=99.9,
        accumulated_evidence=7,
        status=ReflectionStatus.PENDING,
    )
    session.add(queue_item)
    await session.commit()
    await session.refresh(queue_item)

    # Retrieve
    stmt = select(ReflectionQueue).where(ReflectionQueue.entity_id == entity.id)
    result = await session.exec(stmt)
    retrieved_item = result.one()

    assert retrieved_item.priority_score == 99.9
    assert isinstance(retrieved_item.priority_score, float)
    assert retrieved_item.accumulated_evidence == 7


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_priority_and_evidence(session: AsyncSession):
    """Test updating the priority score and evidence count."""
    # Setup
    entity = Entity(canonical_name=f'Update Entity {uuid4()}')
    session.add(entity)
    await session.commit()

    queue_item = ReflectionQueue(entity_id=entity.id, priority_score=1.0, accumulated_evidence=0)
    session.add(queue_item)
    await session.commit()

    # Update
    queue_item.priority_score = 50.5
    queue_item.accumulated_evidence += 5
    session.add(queue_item)
    await session.commit()
    await session.refresh(queue_item)

    assert queue_item.priority_score == 50.5
    assert queue_item.accumulated_evidence == 5
