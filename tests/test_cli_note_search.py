import os
import pytest
from unittest.mock import AsyncMock, patch
from typer.testing import CliRunner
from uuid import uuid4
from memex_cli.notes import app
from memex_common.config import MemexConfig
from memex_common.schemas import NoteSearchResult, SectionSummaryDTO

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
        summary=SectionSummaryDTO(
            what='A test document about testing',
            who='Test author',
        ),
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
