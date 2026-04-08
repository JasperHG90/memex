"""Unit tests for ReflectionQueueService.handle_deletion_event."""

import pytest
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.config import ReflectionConfig
from memex_core.memory.sql_models import ReflectionQueue, ReflectionStatus


@pytest.fixture
def service():
    config = ReflectionConfig(weight_urgency=0.5, weight_importance=0.2, weight_resonance=0.3)
    return ReflectionQueueService(config)


@pytest.mark.asyncio
async def test_handle_deletion_event_noop_on_empty(service):
    session = AsyncMock(spec=AsyncSession)
    await service.handle_deletion_event(session, set())
    session.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_deletion_event_sets_priority_and_pending(service):
    session = AsyncMock(spec=AsyncSession)
    entity_id = uuid4()
    vault_id = uuid4()

    queue_item = ReflectionQueue(
        entity_id=entity_id,
        vault_id=vault_id,
        accumulated_evidence=5,
        priority_score=0.3,
        status=ReflectionStatus.PROCESSING,
        retry_count=2,
    )

    # _ensure_queue_items mock (first exec call)
    ensure_result = MagicMock()
    ensure_result.all.return_value = [entity_id]

    # handle_deletion_event fetch (second exec call)
    fetch_result = MagicMock()
    fetch_result.all.return_value = [queue_item]

    session.exec = AsyncMock(side_effect=[ensure_result, fetch_result])

    await service.handle_deletion_event(session, {entity_id}, vault_id)

    assert queue_item.priority_score == 1.0
    assert queue_item.status == ReflectionStatus.PENDING
    assert queue_item.retry_count == 0
    # accumulated_evidence should NOT be changed
    assert queue_item.accumulated_evidence == 5
    session.flush.assert_awaited()


@pytest.mark.asyncio
async def test_handle_deletion_event_creates_missing_queue_items(service):
    """When no queue item exists, _ensure_queue_items creates one, then it gets updated."""
    session = AsyncMock(spec=AsyncSession)
    entity_id = uuid4()
    vault_id = uuid4()

    # _ensure_queue_items: no existing items
    ensure_result = MagicMock()
    ensure_result.all.return_value = []

    # After _ensure_queue_items creates the item, the fetch returns it
    new_item = ReflectionQueue(
        entity_id=entity_id,
        vault_id=vault_id,
        accumulated_evidence=0,
        priority_score=1.0,
        status=ReflectionStatus.PENDING,
    )
    fetch_result = MagicMock()
    fetch_result.all.return_value = [new_item]

    session.exec = AsyncMock(side_effect=[ensure_result, fetch_result])

    await service.handle_deletion_event(session, {entity_id}, vault_id)

    # _ensure_queue_items should have added the missing item
    assert session.add.call_count >= 1
    assert new_item.priority_score == 1.0
    assert new_item.status == ReflectionStatus.PENDING


@pytest.mark.asyncio
async def test_handle_deletion_event_revives_dead_letter(service):
    """Dead-lettered items should be reset to PENDING with retry_count=0."""
    session = AsyncMock(spec=AsyncSession)
    entity_id = uuid4()
    vault_id = uuid4()

    queue_item = ReflectionQueue(
        entity_id=entity_id,
        vault_id=vault_id,
        accumulated_evidence=3,
        priority_score=0.1,
        status=ReflectionStatus.DEAD_LETTER,
        retry_count=5,
    )

    ensure_result = MagicMock()
    ensure_result.all.return_value = [entity_id]

    fetch_result = MagicMock()
    fetch_result.all.return_value = [queue_item]

    session.exec = AsyncMock(side_effect=[ensure_result, fetch_result])

    await service.handle_deletion_event(session, {entity_id}, vault_id)

    assert queue_item.priority_score == 1.0
    assert queue_item.status == ReflectionStatus.PENDING
    assert queue_item.retry_count == 0
    # accumulated_evidence should remain unchanged
    assert queue_item.accumulated_evidence == 3
