import pytest
from unittest.mock import AsyncMock, patch
from typer.testing import CliRunner
from uuid import uuid4
from memex_cli.notes import app
from memex_common.schemas import NoteSearchResult, NoteSnippet

runner = CliRunner()


@pytest.fixture
def mock_api():
    api = AsyncMock()
    return api


def test_cli_note_search_success(mock_api):
    """Test 'memex note search' command with valid results."""

    # Create mock results using Pydantic models
    doc_id = uuid4()
    snippet_id = uuid4()

    result = NoteSearchResult(
        note_id=doc_id,
        metadata={'title': 'Test Note', 'filename': 'test.md'},
        snippets=[
            NoteSnippet(
                id=snippet_id, text='This is a test snippet.', event_date='2023-01-01T12:00:00'
            )
        ],
        score=0.95,
    )

    mock_api.search_notes.return_value = [result]

    # Mock get_api_context to yield our mock api
    with patch('memex_cli.notes.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'])

        assert result.exit_code == 0
        assert 'Test Note' in result.stdout
        assert str(doc_id) in result.stdout
        assert 'This is a test' in result.stdout
        assert 'snippet.' in result.stdout


def test_cli_note_search_empty(mock_api):
    """Test 'memex note search' command with no results."""

    mock_api.search_notes.return_value = []

    with patch('memex_cli.notes.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'])

        assert result.exit_code == 0
        assert 'No notes found' in result.stdout


def test_cli_note_search_error(mock_api):
    """Test 'memex note search' command handling API errors."""

    mock_api.search_notes.side_effect = Exception('API Error')

    with patch('memex_cli.notes.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'])

        assert result.exit_code == 1
        assert 'Error: API Error' in result.stdout
