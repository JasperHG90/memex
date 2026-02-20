import pytest
from uuid import uuid4
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.config import ReflectionConfig
from memex_core.memory.sql_models import Entity, ReflectionQueue


@pytest.fixture
def queue_service():
    config = ReflectionConfig(
        weight_urgency=1.0,  # Simple weights for testing
        weight_importance=0.0,
        weight_resonance=0.0,
    )
    return ReflectionQueueService(config)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_extraction_flow_integration(
    session: AsyncSession, queue_service: ReflectionQueueService
):
    """Test that extracting entities updates the queue in the DB."""
    # 1. Create Entity
    entity = Entity(canonical_name=f'Extraction Flow {uuid4()}')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    # 2. Trigger Extraction Event
    await queue_service.handle_extraction_event(session, {entity.id})

    # 3. Verify Queue Item created
    stmt = select(ReflectionQueue).where(ReflectionQueue.entity_id == entity.id)
    result = await session.exec(stmt)
    queue_item = result.one()

    assert queue_item.accumulated_evidence == 1
    assert queue_item.priority_score == 1.0  # 1.0 * 1 + 0 + 0

    # 4. Trigger again
    await queue_service.handle_extraction_event(session, {entity.id})
    await session.refresh(queue_item)

    assert queue_item.accumulated_evidence == 2
    assert queue_item.priority_score == 2.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_retrieval_flow_integration(
    session: AsyncSession, queue_service: ReflectionQueueService
):
    """Test that retrieving entities updates the queue and entity stats."""
    # 1. Create Entity
    entity = Entity(canonical_name=f'Retrieval Flow {uuid4()}')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    # 2. Trigger Retrieval Event
    # Change config to weight resonance
    queue_service.config.weight_urgency = 0.0
    queue_service.config.weight_resonance = 1.0

    await queue_service.handle_retrieval_event(session, {entity.id})

    # 3. Verify Entity Updated
    await session.refresh(entity)
    assert entity.retrieval_count == 1
    assert entity.last_retrieved_at is not None

    # 4. Verify Queue Item Created
    stmt = select(ReflectionQueue).where(ReflectionQueue.entity_id == entity.id)
    result = await session.exec(stmt)
    queue_item = result.one()

    # Priority = log10(1) * 1.0 = 0
    assert queue_item.priority_score == 0.0

    # Trigger 9 more times to reach 10 (log10(10) = 1)
    for _ in range(9):
        await queue_service.handle_retrieval_event(session, {entity.id})

    await session.refresh(queue_item)
    await session.refresh(entity)

    assert entity.retrieval_count == 10
    assert queue_item.priority_score == 1.0  # log10(10) * 1.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_batch_extraction_integration(
    session: AsyncSession, queue_service: ReflectionQueueService
):
    """Test updating multiple entities in a single batch."""
    # 1. Create 5 entities
    entities = [Entity(canonical_name=f'Batch {i} {uuid4()}') for i in range(5)]
    for e in entities:
        session.add(e)
    await session.commit()
    for e in entities:
        await session.refresh(e)

    entity_ids = {e.id for e in entities}

    # 2. Trigger Batch Extraction
    await queue_service.handle_extraction_event(session, entity_ids)

    # 3. Verify all have queue items
    stmt = select(ReflectionQueue).where(col(ReflectionQueue.entity_id).in_(entity_ids))
    result = await session.exec(stmt)
    queue_items = result.all()

    assert len(queue_items) == 5
    for item in queue_items:
        assert item.accumulated_evidence == 1
