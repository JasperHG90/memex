import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.config import ReflectionConfig
from memex_core.memory.sql_models import Entity, ReflectionQueue, ReflectionStatus


@pytest.fixture
def service():
    config = ReflectionConfig(weight_urgency=0.5, weight_importance=0.2, weight_resonance=0.3)
    return ReflectionQueueService(config)


def test_calculate_priority(service):
    # Case 1: Brand new entity (0 evidence, 1 mention, 0 retrieval)
    # Score = (0.5 * 0) + (0.2 * log10(1)) + (0.3 * 0) = 0 + 0 + 0 = 0
    assert service.calculate_priority(0, 1, 0) == 0.0

    # Case 2: Urgency (10 evidence, 1 mention, 0 retrieval)
    # Score = (0.5 * 10) + 0 + 0 = 5.0
    assert service.calculate_priority(10, 1, 0) == 5.0

    # Case 3: Importance (0 evidence, 100 mentions, 0 retrieval)
    # Score = 0 + (0.2 * log10(100)) + 0 = 0.2 * 2 = 0.4
    assert service.calculate_priority(0, 100, 0) == 0.4

    # Case 4: Resonance (0 evidence, 1 mention, 10 retrievals)
    # Score = 0 + 0 + (0.3 * log10(10)) = 0.3
    assert service.calculate_priority(0, 1, 10) == 0.3

    # Case 5: Combined
    # Evidence=4 (2.0), Mentions=100 (0.4), Retrievals=10 (0.3)
    # Total = 2.7
    assert service.calculate_priority(4, 100, 10) == 2.7


@pytest.mark.asyncio
async def test_handle_extraction_event(service):
    session = AsyncMock(spec=AsyncSession)
    entity_id = uuid4()

    # Mock DB return
    entity = Entity(id=entity_id, mention_count=10, retrieval_count=0)
    queue_item = ReflectionQueue(entity_id=entity_id, accumulated_evidence=5, priority_score=1.0)

    # Mock exec().all()
    mock_result = MagicMock()
    mock_result.all.return_value = [(entity, queue_item)]
    session.exec.return_value = mock_result

    await service.handle_extraction_event(session, {entity_id})

    # Verify evidence incremented
    assert queue_item.accumulated_evidence == 6
    # Verify priority updated
    # Score = (0.5 * 6) + (0.2 * 1) + 0 = 3.2
    assert queue_item.priority_score == 3.2
    assert queue_item.status == ReflectionStatus.PENDING

    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_extraction_new_item(service):
    """Test creating a new ReflectionQueue item if one doesn't exist."""
    session = AsyncMock(spec=AsyncSession)
    entity_id = uuid4()

    # Mock DB return: Entity exists, QueueItem is None
    entity = Entity(id=entity_id, mention_count=5)

    mock_result = MagicMock()
    mock_result.all.return_value = [(entity, None)]
    session.exec.return_value = mock_result

    await service.handle_extraction_event(session, {entity_id})

    # Verify session.add was called with a NEW ReflectionQueue object
    assert session.add.call_count >= 1
    args, _ = session.add.call_args
    new_item = args[0]

    assert isinstance(new_item, ReflectionQueue)
    assert new_item.entity_id == entity_id
    assert new_item.accumulated_evidence == 1
    assert new_item.status == ReflectionStatus.PENDING


@pytest.mark.asyncio
async def test_handle_retrieval_event(service):
    session = AsyncMock(spec=AsyncSession)
    entity_id = uuid4()

    # Mock DB return — queue item is PROCESSING (mid-reflection)
    entity = Entity(id=entity_id, mention_count=100, retrieval_count=9)
    queue_item = ReflectionQueue(
        entity_id=entity_id,
        accumulated_evidence=0,
        priority_score=0.0,
        status=ReflectionStatus.PROCESSING,
    )

    # Mock exec().all()
    mock_result = MagicMock()
    mock_result.all.return_value = [(entity, queue_item)]
    session.exec.return_value = mock_result

    await service.handle_retrieval_event(session, {entity_id})

    # Verify retrieval count incremented
    assert entity.retrieval_count == 10
    assert entity.last_retrieved_at is not None

    # Verify priority updated (Evidence=0, Mentions=100 -> log=2 (0.4), Retrievals=10 -> log=1 (0.3))
    # Score = 0 + 0.4 + 0.3 = 0.7
    assert queue_item.priority_score == 0.7

    # Retrieval should NOT re-queue for reflection — no new evidence was added
    assert queue_item.status == ReflectionStatus.PROCESSING


@pytest.mark.asyncio
async def test_claim_next_batch_filtering_logic():
    """
    Verify that the claim_next_batch constructs a query with the priority filter.
    Note: Inspecting SQLAlchemy statement objects is complex, so we will do a basic check
    or better, creating a small integration test if possible.
    Given the constraints, I will create a unit test that mocks the result
    assuming the query worked, but that's tautological.

    Better approach:
    I will write a test that acts as an integration test if I can, or
    I will rely on the fact that I modified the code correctly.

    Let's check `test_queue_service.py` again. It uses AsyncMock.
    Let's add a test that inspects the `where` clauses of the constructed statement if possible,
    or at least ensures `session.exec` is called.
    """
    config = ReflectionConfig(min_priority=0.5)
    svc = ReflectionQueueService(config)
    session = AsyncMock(spec=AsyncSession)

    # Mock result to return nothing, just to check the call
    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.exec.return_value = mock_result

    await svc.claim_next_batch(session, limit=10)

    # Get the statement passed to exec
    assert session.exec.called
    stmt = session.exec.call_args[0][0]

    # Convert statement to string to check for the presence of the priority logic
    # This is a bit brittle but confirms the clause was added.
    # The actual string representation of a SQLModel select statement might vary,
    # but it usually contains the WHERE clauses.
    # However, uncompiled statements might not show values.
    # Let's just check if we can verify the logic structure.
    # A better way is to check the `whereclause` attribute of the statement.

    # We can check if 'priority_score' is in the string representation of the query
    assert 'priority_score' in str(stmt)
    assert (
        '0.5' in str(stmt)
        or 'min_priority' in str(stmt)
        or 'priority_score >= :priority_score_1' in str(stmt)
    )


@pytest.mark.asyncio
async def test_recover_stale_processing():
    """Stale PROCESSING items should be reset to PENDING."""
    config = ReflectionConfig(stale_processing_timeout_seconds=600)
    svc = ReflectionQueueService(config)
    session = AsyncMock(spec=AsyncSession)

    stale_item = ReflectionQueue(
        entity_id=uuid4(),
        status=ReflectionStatus.PROCESSING,
        last_queued_at=datetime.now(timezone.utc) - timedelta(hours=1),
        priority_score=0.5,
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [stale_item]
    session.exec.return_value = mock_result

    recovered = await svc.recover_stale_processing(session)

    assert recovered == 1
    assert stale_item.status == ReflectionStatus.PENDING
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_recover_stale_processing_no_stale_items():
    """No recovery needed when nothing is stale."""
    config = ReflectionConfig(stale_processing_timeout_seconds=600)
    svc = ReflectionQueueService(config)
    session = AsyncMock(spec=AsyncSession)

    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.exec.return_value = mock_result

    recovered = await svc.recover_stale_processing(session)

    assert recovered == 0
    session.commit.assert_not_awaited()
