"""Tests for retry counter and dead letter queue for reflection tasks."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.memory.sql_models import ReflectionQueue, ReflectionStatus


# ---------------------------------------------------------------------------
# ReflectionStatus enum tests
# ---------------------------------------------------------------------------


class TestReflectionStatus:
    """Tests for the DEAD_LETTER status addition."""

    def test_dead_letter_exists(self):
        assert ReflectionStatus.DEAD_LETTER == 'dead_letter'

    def test_all_statuses(self):
        values = {s.value for s in ReflectionStatus}
        assert values == {'pending', 'processing', 'failed', 'dead_letter'}


# ---------------------------------------------------------------------------
# ReflectionQueue model tests
# ---------------------------------------------------------------------------


class TestReflectionQueueModel:
    """Tests for the new DLQ fields on ReflectionQueue."""

    def test_default_retry_count(self):
        item = ReflectionQueue(
            entity_id=uuid4(),
            vault_id=uuid4(),
            status=ReflectionStatus.PENDING,
        )
        assert item.retry_count == 0

    def test_default_max_retries(self):
        item = ReflectionQueue(
            entity_id=uuid4(),
            vault_id=uuid4(),
            status=ReflectionStatus.PENDING,
        )
        assert item.max_retries == 3

    def test_default_last_error_is_none(self):
        item = ReflectionQueue(
            entity_id=uuid4(),
            vault_id=uuid4(),
            status=ReflectionStatus.PENDING,
        )
        assert item.last_error is None

    def test_custom_retry_fields(self):
        item = ReflectionQueue(
            entity_id=uuid4(),
            vault_id=uuid4(),
            status=ReflectionStatus.FAILED,
            retry_count=2,
            max_retries=5,
            last_error='Connection timeout',
        )
        assert item.retry_count == 2
        assert item.max_retries == 5
        assert item.last_error == 'Connection timeout'

    def test_dead_letter_status(self):
        item = ReflectionQueue(
            entity_id=uuid4(),
            vault_id=uuid4(),
            status=ReflectionStatus.DEAD_LETTER,
            retry_count=3,
            max_retries=3,
            last_error='Max retries exceeded',
        )
        assert item.status == ReflectionStatus.DEAD_LETTER


# ---------------------------------------------------------------------------
# ReflectionQueueService.mark_failed tests
# ---------------------------------------------------------------------------


class TestMarkFailed:
    """Tests for ReflectionQueueService.mark_failed."""

    @pytest.fixture()
    def mock_session(self):
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        return session

    @pytest.fixture()
    def queue_service(self):
        from memex_core.config import ReflectionConfig

        from memex_core.memory.reflect.queue_service import ReflectionQueueService

        return ReflectionQueueService(ReflectionConfig())

    @pytest.mark.asyncio
    async def test_first_failure_stays_failed(self, queue_service, mock_session):
        """First failure should increment retry_count and set status to FAILED."""
        entity_id = uuid4()
        vault_id = uuid4()
        item = ReflectionQueue(
            entity_id=entity_id,
            vault_id=vault_id,
            status=ReflectionStatus.PROCESSING,
            retry_count=0,
            max_retries=3,
        )

        mock_result = MagicMock()
        mock_result.first.return_value = item
        mock_session.exec = AsyncMock(return_value=mock_result)

        await queue_service.mark_failed(
            mock_session, entity_id=entity_id, vault_id=vault_id, error='LLM timeout'
        )

        assert item.retry_count == 1
        assert item.status == ReflectionStatus.FAILED
        assert item.last_error == 'LLM timeout'
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_max_retries_moves_to_dead_letter(self, queue_service, mock_session):
        """When retry_count reaches max_retries, status should become DEAD_LETTER."""
        entity_id = uuid4()
        vault_id = uuid4()
        item = ReflectionQueue(
            entity_id=entity_id,
            vault_id=vault_id,
            status=ReflectionStatus.PROCESSING,
            retry_count=2,
            max_retries=3,
        )

        mock_result = MagicMock()
        mock_result.first.return_value = item
        mock_session.exec = AsyncMock(return_value=mock_result)

        await queue_service.mark_failed(
            mock_session, entity_id=entity_id, vault_id=vault_id, error='Final failure'
        )

        assert item.retry_count == 3
        assert item.status == ReflectionStatus.DEAD_LETTER
        assert item.last_error == 'Final failure'

    @pytest.mark.asyncio
    async def test_mark_failed_truncates_long_errors(self, queue_service, mock_session):
        """Error messages longer than 2000 chars should be truncated."""
        entity_id = uuid4()
        vault_id = uuid4()
        item = ReflectionQueue(
            entity_id=entity_id,
            vault_id=vault_id,
            status=ReflectionStatus.PROCESSING,
            retry_count=0,
            max_retries=3,
        )

        mock_result = MagicMock()
        mock_result.first.return_value = item
        mock_session.exec = AsyncMock(return_value=mock_result)

        long_error = 'x' * 5000
        await queue_service.mark_failed(
            mock_session, entity_id=entity_id, vault_id=vault_id, error=long_error
        )

        assert len(item.last_error) == 2000

    @pytest.mark.asyncio
    async def test_mark_failed_item_not_found(self, queue_service, mock_session):
        """Should be a no-op if the item doesn't exist."""
        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_session.exec = AsyncMock(return_value=mock_result)

        await queue_service.mark_failed(
            mock_session, entity_id=uuid4(), vault_id=uuid4(), error='test'
        )

        mock_session.add.assert_not_called()
        mock_session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# ReflectionQueueService.get_dead_letter_items tests
# ---------------------------------------------------------------------------


class TestGetDeadLetterItems:
    """Tests for ReflectionQueueService.get_dead_letter_items."""

    @pytest.fixture()
    def mock_session(self):
        return AsyncMock()

    @pytest.fixture()
    def queue_service(self):
        from memex_core.config import ReflectionConfig

        from memex_core.memory.reflect.queue_service import ReflectionQueueService

        return ReflectionQueueService(ReflectionConfig())

    @pytest.mark.asyncio
    async def test_returns_dead_letter_items(self, queue_service, mock_session):
        dlq_item = ReflectionQueue(
            entity_id=uuid4(),
            vault_id=uuid4(),
            status=ReflectionStatus.DEAD_LETTER,
            retry_count=3,
            max_retries=3,
            last_error='LLM down',
        )
        mock_result = MagicMock()
        mock_result.all.return_value = [dlq_item]
        mock_session.exec = AsyncMock(return_value=mock_result)

        items = await queue_service.get_dead_letter_items(mock_session, limit=10)
        assert len(items) == 1
        assert items[0].status == ReflectionStatus.DEAD_LETTER

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_dlq(self, queue_service, mock_session):
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_result)

        items = await queue_service.get_dead_letter_items(mock_session)
        assert items == []


# ---------------------------------------------------------------------------
# ReflectionQueueService.retry_dead_letter tests
# ---------------------------------------------------------------------------


class TestRetryDeadLetter:
    """Tests for ReflectionQueueService.retry_dead_letter."""

    @pytest.fixture()
    def mock_session(self):
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        return session

    @pytest.fixture()
    def queue_service(self):
        from memex_core.config import ReflectionConfig

        from memex_core.memory.reflect.queue_service import ReflectionQueueService

        return ReflectionQueueService(ReflectionConfig())

    @pytest.mark.asyncio
    async def test_retry_resets_to_pending(self, queue_service, mock_session):
        item_id = uuid4()
        item = ReflectionQueue(
            id=item_id,
            entity_id=uuid4(),
            vault_id=uuid4(),
            status=ReflectionStatus.DEAD_LETTER,
            retry_count=3,
            max_retries=3,
            last_error='Previous error',
        )
        mock_session.get = AsyncMock(return_value=item)

        result = await queue_service.retry_dead_letter(mock_session, item_id)

        assert result is not None
        assert result.status == ReflectionStatus.PENDING
        assert result.retry_count == 0
        assert result.last_error is None
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retry_not_found_returns_none(self, queue_service, mock_session):
        mock_session.get = AsyncMock(return_value=None)

        result = await queue_service.retry_dead_letter(mock_session, uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_retry_non_dead_letter_returns_none(self, queue_service, mock_session):
        """Can only retry items in DEAD_LETTER status."""
        item = ReflectionQueue(
            entity_id=uuid4(),
            vault_id=uuid4(),
            status=ReflectionStatus.PENDING,
        )
        mock_session.get = AsyncMock(return_value=item)

        result = await queue_service.retry_dead_letter(mock_session, uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# DeadLetterItemDTO tests
# ---------------------------------------------------------------------------


class TestDeadLetterItemDTO:
    """Tests for the DeadLetterItemDTO schema."""

    def test_dto_creation(self):
        from memex_common.schemas import DeadLetterItemDTO

        item_id = uuid4()
        entity_id = uuid4()
        vault_id = uuid4()

        dto = DeadLetterItemDTO(
            id=item_id,
            entity_id=entity_id,
            vault_id=vault_id,
            priority_score=0.5,
            retry_count=3,
            max_retries=3,
            last_error='Test error',
            status='dead_letter',
        )
        assert dto.id == item_id
        assert dto.entity_id == entity_id
        assert dto.retry_count == 3
        assert dto.status == 'dead_letter'

    def test_dto_defaults(self):
        from memex_common.schemas import DeadLetterItemDTO

        dto = DeadLetterItemDTO(
            id=uuid4(),
            entity_id=uuid4(),
            vault_id=uuid4(),
        )
        assert dto.priority_score == 0.0
        assert dto.retry_count == 0
        assert dto.max_retries == 3
        assert dto.last_error is None
        assert dto.status == 'dead_letter'
