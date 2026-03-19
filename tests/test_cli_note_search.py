import os
import pytest
from unittest.mock import AsyncMock, patch
from typer.testing import CliRunner
from uuid import uuid4
from memex_cli.notes import app
from memex_common.config import MemexConfig
from memex_common.schemas import BlockSummaryDTO, NoteSearchResult

runner = CliRunner()


@pytest.fixture
def mock_api():
    api = AsyncMock()
    return api


@pytest.fixture
def mock_config():
    with patch.dict(
        os.environ, {'MEMEX_LOAD_LOCAL_CONFIG': 'false', 'MEMEX_LOAD_GLOBAL_CONFIG': 'false'}
    ):
        return MemexConfig()


def test_cli_note_search_success(mock_api, mock_config):
    """Test 'memex note search' command with valid results."""

    # Create mock results using Pydantic models
    doc_id = uuid4()

    result = NoteSearchResult(
        note_id=doc_id,
        metadata={'title': 'Test Note', 'filename': 'test.md'},
        summaries=[
            BlockSummaryDTO(
                topic='A test document about testing',
                key_points=['Written by test author'],
            )
        ],
        score=0.95,
    )

    mock_api.search_notes.return_value = [result]

    # Mock get_api_context to yield our mock api
    with patch('memex_cli.notes.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'], obj=mock_config)

        assert result.exit_code == 0
        assert 'Test Note' in result.stdout
        assert str(doc_id) in result.stdout


def test_cli_note_search_empty(mock_api, mock_config):
    """Test 'memex note search' command with no results."""

    mock_api.search_notes.return_value = []

    with patch('memex_cli.notes.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'], obj=mock_config)

        assert result.exit_code == 0
        assert 'No notes found' in result.stdout


def test_cli_note_search_error(mock_api, mock_config):
    """Test 'memex note search' command handling API errors."""

    mock_api.search_notes.side_effect = Exception('API Error')

    with patch('memex_cli.notes.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'], obj=mock_config)

        assert result.exit_code == 1
        assert 'Error: API Error' in result.stdout


def test_cli_note_search_preview_shows_topics(mock_api, mock_config):
    """Preview column should show block summary topics joined by ' | '."""
    doc_id = uuid4()

    result = NoteSearchResult(
        note_id=doc_id,
        metadata={'title': 'Multi-block Note'},
        summaries=[
            BlockSummaryDTO(topic='Introduction', key_points=['Background']),
            BlockSummaryDTO(topic='Methodology', key_points=['Approach A']),
        ],
        score=0.88,
    )

    mock_api.search_notes.return_value = [result]

    with patch('memex_cli.notes.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        cli_result = runner.invoke(app, ['search', 'multi'], obj=mock_config)

        assert cli_result.exit_code == 0
        assert 'Introduction' in cli_result.stdout
        assert 'Methodology' in cli_result.stdout


def test_cli_note_search_no_summaries_preview(mock_api, mock_config):
    """When no summaries, preview should show '[No preview available]'."""
    doc_id = uuid4()

    result = NoteSearchResult(
        note_id=doc_id,
        metadata={'title': 'Empty Preview'},
        summaries=[],
        score=0.5,
    )

    mock_api.search_notes.return_value = [result]

    with patch('memex_cli.notes.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        cli_result = runner.invoke(app, ['search', 'empty'], obj=mock_config)

        assert cli_result.exit_code == 0
        # Rich table may wrap text across lines; check individual words
        assert 'No preview' in cli_result.stdout
        assert 'available' in cli_result.stdout
