import pytest
import asyncio
from memex_core.memory.sql_models import Entity, ReflectionQueue, ReflectionStatus
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.config import ReflectionConfig


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_next_batch_concurrency(session_manager):
    """
    Verify that multiple workers (sessions) claiming batches concurrently
    do not receive the same items.
    """
    # 1. Setup: Create 10 pending tasks
    async with session_manager() as session:
        entities = []
        for i in range(10):
            entity = Entity(canonical_name=f'Entity {i}')
            session.add(entity)
            entities.append(entity)
        await session.commit()

        for entity in entities:
            queue_item = ReflectionQueue(
                entity_id=entity.id,
                status=ReflectionStatus.PENDING,
                priority_score=1.0,
            )
            session.add(queue_item)
        await session.commit()

    # 2. Parallel claiming
    config = ReflectionConfig()
    service = ReflectionQueueService(config)

    async def claim_worker(worker_id):
        async with session_manager() as worker_session:
            # We use a small limit to force multiple calls
            return await service.claim_next_batch(worker_session, limit=3)

    # Run 4 workers in parallel
    results = await asyncio.gather(
        claim_worker(1), claim_worker(2), claim_worker(3), claim_worker(4)
    )

    # 3. Verify
    all_claimed_ids = []
    for batch in results:
        for item in batch:
            all_claimed_ids.append(item.id)
            assert item.status == ReflectionStatus.PROCESSING

    # Check for duplicates
    assert len(all_claimed_ids) == 10
    assert len(set(all_claimed_ids)) == 10


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_next_batch_marks_processing(session, session_manager):
    """
    Verify that claimed items are indeed marked as PROCESSING in the DB.
    """
    # 1. Setup
    entity = Entity(canonical_name='Single Entity')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)

    queue_item = ReflectionQueue(
        entity_id=entity.id,
        status=ReflectionStatus.PENDING,
        priority_score=5.0,
    )
    session.add(queue_item)
    await session.commit()
    await session.refresh(queue_item)

    # 2. Claim
    config = ReflectionConfig()
    service = ReflectionQueueService(config)

    async with session_manager() as worker_session:
        claimed = await service.claim_next_batch(worker_session, limit=1)
        assert len(claimed) == 1
        assert claimed[0].id == queue_item.id
        assert claimed[0].status == ReflectionStatus.PROCESSING

    # 3. Verify in separate session
    async with session_manager() as verify_session:
        db_item = await verify_session.get(ReflectionQueue, queue_item.id)
        assert db_item.status == ReflectionStatus.PROCESSING
