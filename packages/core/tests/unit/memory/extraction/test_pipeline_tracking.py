"""Unit tests for extraction pipeline tracking module."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.extraction.models import RetainContent
from memex_core.memory.extraction.pipeline.tracking import (
    enqueue_for_reflection,
    track_document,
)


# ===========================================================================
# track_document tests
# ===========================================================================


class TestTrackDocument:
    """Tests for the track_document pipeline function."""

    @pytest.mark.asyncio
    async def test_calls_storage_with_correct_params(self) -> None:
        """Verify track_document delegates to storage.handle_document_tracking."""
        session = AsyncMock()
        note_id = f'note-{uuid4()}'
        vault_id = uuid4()
        contents = [
            RetainContent(
                content='Hello world',
                vault_id=vault_id,
                event_date=dt.datetime(2024, 1, 15, tzinfo=dt.timezone.utc),
                payload={'key': 'value'},
            )
        ]

        with patch(
            'memex_core.memory.extraction.pipeline.tracking.storage.handle_document_tracking',
            new_callable=AsyncMock,
        ) as mock_storage:
            await track_document(session, note_id, contents, is_first_batch=True, vault_id=vault_id)

            mock_storage.assert_called_once()
            call_kwargs = mock_storage.call_args
            assert call_kwargs[0][0] is session
            assert call_kwargs[0][1] == note_id
            assert call_kwargs[0][2] == 'Hello world'
            assert call_kwargs[0][3] is True  # is_first_batch

    @pytest.mark.asyncio
    async def test_combines_multiple_contents(self) -> None:
        """Verify content from multiple items is joined with newlines."""
        session = AsyncMock()
        note_id = f'note-{uuid4()}'
        contents = [
            RetainContent(content='First part'),
            RetainContent(content='Second part'),
        ]

        with patch(
            'memex_core.memory.extraction.pipeline.tracking.storage.handle_document_tracking',
            new_callable=AsyncMock,
        ) as mock_storage:
            await track_document(session, note_id, contents, is_first_batch=False)

            combined = mock_storage.call_args[0][2]
            assert combined == 'First part\nSecond part'

    @pytest.mark.asyncio
    async def test_extracts_assets_from_payload(self) -> None:
        """Verify assets are extracted from the first content's payload."""
        session = AsyncMock()
        note_id = f'note-{uuid4()}'
        contents = [
            RetainContent(
                content='Content with assets',
                payload={'assets': ['/path/to/file.png', '/path/to/data.csv']},
            )
        ]

        with patch(
            'memex_core.memory.extraction.pipeline.tracking.storage.handle_document_tracking',
            new_callable=AsyncMock,
        ) as mock_storage:
            await track_document(session, note_id, contents, is_first_batch=True)

            call_kwargs = mock_storage.call_args[1]
            assert call_kwargs['assets'] == ['/path/to/file.png', '/path/to/data.csv']

    @pytest.mark.asyncio
    async def test_empty_contents(self) -> None:
        """Verify track_document handles empty contents list gracefully."""
        session = AsyncMock()
        note_id = f'note-{uuid4()}'

        with patch(
            'memex_core.memory.extraction.pipeline.tracking.storage.handle_document_tracking',
            new_callable=AsyncMock,
        ) as mock_storage:
            await track_document(session, note_id, [], is_first_batch=True)

            mock_storage.assert_called_once()
            combined = mock_storage.call_args[0][2]
            assert combined == ''

    @pytest.mark.asyncio
    async def test_default_vault_id(self) -> None:
        """Verify default vault_id is GLOBAL_VAULT_ID."""
        session = AsyncMock()
        note_id = f'note-{uuid4()}'
        contents = [RetainContent(content='test')]

        with patch(
            'memex_core.memory.extraction.pipeline.tracking.storage.handle_document_tracking',
            new_callable=AsyncMock,
        ) as mock_storage:
            await track_document(session, note_id, contents, is_first_batch=True)

            call_kwargs = mock_storage.call_args[1]
            assert call_kwargs['vault_id'] == GLOBAL_VAULT_ID

    @pytest.mark.asyncio
    async def test_content_fingerprint_passed(self) -> None:
        """Verify content_fingerprint from payload is forwarded."""
        session = AsyncMock()
        note_id = f'note-{uuid4()}'
        contents = [
            RetainContent(
                content='test',
                payload={'content_fingerprint': 'abc123'},
            )
        ]

        with patch(
            'memex_core.memory.extraction.pipeline.tracking.storage.handle_document_tracking',
            new_callable=AsyncMock,
        ) as mock_storage:
            await track_document(session, note_id, contents, is_first_batch=True)

            call_kwargs = mock_storage.call_args[1]
            assert call_kwargs['content_fingerprint'] == 'abc123'

    @pytest.mark.asyncio
    async def test_passes_description_from_payload(self) -> None:
        """Verify note_description from payload is forwarded as description kwarg."""
        session = AsyncMock()
        note_id = f'note-{uuid4()}'
        contents = [
            RetainContent(
                content='test',
                payload={'note_description': 'My desc', 'tags': ['t1']},
            )
        ]

        with patch(
            'memex_core.memory.extraction.pipeline.tracking.storage.handle_document_tracking',
            new_callable=AsyncMock,
        ) as mock_storage:
            await track_document(session, note_id, contents, is_first_batch=True)

            call_kwargs = mock_storage.call_args[1]
            assert call_kwargs['description'] == 'My desc'


# ===========================================================================
# enqueue_for_reflection tests
# ===========================================================================


class TestEnqueueForReflection:
    """Tests for the enqueue_for_reflection pipeline function."""

    @pytest.mark.asyncio
    async def test_calls_queue_service(self) -> None:
        """Verify enqueue calls queue_service.handle_extraction_event."""
        session = AsyncMock()
        vault_id = uuid4()
        entity_ids = {uuid4(), uuid4()}
        queue_service = AsyncMock()

        await enqueue_for_reflection(session, entity_ids, vault_id, queue_service)

        queue_service.handle_extraction_event.assert_called_once_with(
            session, entity_ids, vault_id=vault_id
        )

    @pytest.mark.asyncio
    async def test_noop_when_queue_service_is_none(self) -> None:
        """Verify enqueue is a no-op when queue_service is None."""
        session = AsyncMock()
        vault_id = uuid4()
        entity_ids = {uuid4()}

        # Should not raise
        await enqueue_for_reflection(session, entity_ids, vault_id, None)

    @pytest.mark.asyncio
    async def test_noop_when_entity_ids_empty(self) -> None:
        """Verify enqueue is a no-op when touched_entity_ids is empty."""
        session = AsyncMock()
        vault_id = uuid4()
        queue_service = AsyncMock()

        await enqueue_for_reflection(session, set(), vault_id, queue_service)

        queue_service.handle_extraction_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_both_none_and_empty(self) -> None:
        """Verify enqueue handles both None service and empty entity set."""
        session = AsyncMock()
        vault_id = uuid4()

        await enqueue_for_reflection(session, set(), vault_id, None)
        # No assertion needed — just verifying no exception
