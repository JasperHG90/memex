"""Unit tests for MemexAPI.update_user_notes (Feature C: AC-C01)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.api import inject_user_notes


# ---------------------------------------------------------------------------
# inject_user_notes helper tests
# ---------------------------------------------------------------------------


def test_inject_user_notes_into_existing_frontmatter():
    content = '---\ntitle: Test\n---\nBody text.'
    result = inject_user_notes(content, 'My annotation')
    assert 'user_notes: |' in result
    assert 'My annotation' in result
    assert 'title: Test' in result
    assert 'Body text.' in result


def test_inject_user_notes_creates_frontmatter():
    content = 'Body text without frontmatter.'
    result = inject_user_notes(content, 'My annotation')
    assert result.startswith('---\n')
    assert 'user_notes: |' in result
    assert 'My annotation' in result


def test_inject_user_notes_replaces_existing():
    content = '---\ntitle: Test\nuser_notes: |\n  Old notes\n---\nBody.'
    result = inject_user_notes(content, 'New notes')
    assert 'New notes' in result
    assert 'Old notes' not in result


def test_inject_user_notes_noop_for_none():
    content = '---\ntitle: Test\n---\nBody.'
    result = inject_user_notes(content, None)
    assert result == content


def test_inject_user_notes_noop_for_empty():
    content = '---\ntitle: Test\n---\nBody.'
    result = inject_user_notes(content, '   ')
    assert result == content


# ---------------------------------------------------------------------------
# ExtractionEngine.extract_user_notes unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_user_notes_returns_empty_for_blank():
    """extract_user_notes should return empty results for blank input."""
    from memex_core.memory.extraction.engine import ExtractionEngine

    engine = MagicMock(spec=ExtractionEngine)
    engine.extract_user_notes = ExtractionEngine.extract_user_notes.__get__(engine)
    engine.SECONDS_PER_FACT = 10

    session = AsyncMock()
    result = await engine.extract_user_notes(session, '', str(uuid4()), uuid4())
    assert result == ([], set())


@pytest.mark.asyncio
async def test_extract_user_notes_returns_empty_for_whitespace():
    """extract_user_notes should return empty results for whitespace-only input."""
    from memex_core.memory.extraction.engine import ExtractionEngine

    engine = MagicMock(spec=ExtractionEngine)
    engine.extract_user_notes = ExtractionEngine.extract_user_notes.__get__(engine)
    engine.SECONDS_PER_FACT = 10

    session = AsyncMock()
    result = await engine.extract_user_notes(session, '   \n  ', str(uuid4()), uuid4())
    assert result == ([], set())


# ---------------------------------------------------------------------------
# AC-C01: update_user_notes strips old, injects new, updates content_hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_user_notes_strips_and_reinjects():
    """AC-C01: update_user_notes strips old user_notes, injects new, updates content_hash."""
    note_id = uuid4()
    vault_id = uuid4()

    # Create a mock note
    mock_note = MagicMock()
    mock_note.original_text = '---\ntitle: Test\nuser_notes: |\n  Old annotation\n---\nBody text.'
    mock_note.vault_id = vault_id
    mock_note.created_at = None
    mock_note.content_hash = 'old-hash'

    # Mock session
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_note)
    mock_session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    mock_session.commit = AsyncMock()

    # Mock metastore to return session
    mock_metastore = MagicMock()
    mock_metastore.session = MagicMock()

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    mock_metastore.session.return_value = _SessionCtx()

    # Mock extraction engine
    mock_extraction = AsyncMock()
    mock_extraction.extract_user_notes = AsyncMock(return_value=(['unit-1', 'unit-2'], set()))

    # Build a minimal MemexAPI mock
    api = MagicMock()
    api.metastore = mock_metastore
    api._extraction = mock_extraction
    api.queue_service = None

    from memex_core.api import MemexAPI

    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    result = await api.update_user_notes(note_id, 'New annotation')

    assert result['note_id'] == str(note_id)
    assert result['units_deleted'] == 0  # no old units existed
    assert result['units_created'] == 2

    # Verify note was updated
    assert 'New annotation' in mock_note.original_text
    assert 'Old annotation' not in mock_note.original_text
    assert mock_note.content_hash != 'old-hash'


@pytest.mark.asyncio
async def test_update_user_notes_null_deletes_only():
    """AC-C07 (partial): Setting user_notes to None strips frontmatter and deletes units."""
    note_id = uuid4()
    vault_id = uuid4()

    mock_note = MagicMock()
    mock_note.original_text = '---\ntitle: Test\nuser_notes: |\n  Old notes\n---\nBody.'
    mock_note.vault_id = vault_id
    mock_note.created_at = None

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_note)
    mock_session.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    mock_session.commit = AsyncMock()

    mock_metastore = MagicMock()

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    mock_metastore.session.return_value = _SessionCtx()

    mock_extraction = AsyncMock()

    api = MagicMock()
    api.metastore = mock_metastore
    api._extraction = mock_extraction
    api.queue_service = None

    from memex_core.api import MemexAPI

    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    result = await api.update_user_notes(note_id, None)

    assert result['units_deleted'] == 0
    assert result['units_created'] == 0
    # Extraction should NOT have been called
    mock_extraction.extract_user_notes.assert_not_called()
    # user_notes should be stripped from text
    assert 'user_notes' not in mock_note.original_text
    assert 'Old notes' not in mock_note.original_text


@pytest.mark.asyncio
async def test_update_user_notes_not_found():
    """update_user_notes raises ValueError for non-existent note."""
    note_id = uuid4()

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)

    mock_metastore = MagicMock()

    class _SessionCtx:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, *args):
            pass

    mock_metastore.session.return_value = _SessionCtx()

    api = MagicMock()
    api.metastore = mock_metastore

    from memex_core.api import MemexAPI

    api.update_user_notes = MemexAPI.update_user_notes.__get__(api)

    with pytest.raises(ValueError, match='not found'):
        await api.update_user_notes(note_id, 'test')
