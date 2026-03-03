"""Tests for NoteService.get_note_metadata."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_common.exceptions import ResourceNotFoundError
from memex_core.services.notes import NoteService


@pytest.fixture
def note_service():
    """NoteService with mocked dependencies."""
    metastore = MagicMock()
    filestore = MagicMock()
    config = MagicMock()
    vaults = MagicMock()
    return NoteService(metastore=metastore, filestore=filestore, config=config, vaults=vaults)


@pytest.mark.asyncio
async def test_get_note_metadata_returns_metadata(note_service):
    """get_note_metadata returns the metadata dict when page_index has one."""
    note_id = uuid4()
    metadata = {'title': 'Test', 'description': 'Desc', 'tags': ['a']}
    mock_note = MagicMock()
    mock_note.page_index = {'metadata': metadata, 'toc': []}

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_note
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await note_service.get_note_metadata(note_id)
    assert result == metadata


@pytest.mark.asyncio
async def test_get_note_metadata_returns_none_for_no_page_index(note_service):
    """get_note_metadata returns None when the note has no page_index."""
    note_id = uuid4()
    mock_note = MagicMock()
    mock_note.page_index = None

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_note
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await note_service.get_note_metadata(note_id)
    assert result is None


@pytest.mark.asyncio
async def test_get_note_metadata_raises_for_missing_note(note_service):
    """get_note_metadata raises ResourceNotFoundError for a nonexistent note."""
    note_id = uuid4()

    mock_session = AsyncMock()
    mock_session.get.return_value = None
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    with pytest.raises(ResourceNotFoundError):
        await note_service.get_note_metadata(note_id)


@pytest.mark.asyncio
async def test_get_note_metadata_returns_none_when_no_metadata_key(note_service):
    """get_note_metadata returns None when page_index exists but has no 'metadata' key."""
    note_id = uuid4()
    mock_note = MagicMock()
    mock_note.page_index = {'toc': []}  # no 'metadata' key

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_note
    note_service.metastore.session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    note_service.metastore.session.return_value.__aexit__ = AsyncMock(return_value=False)

    result = await note_service.get_note_metadata(note_id)
    assert result is None
