import pytest
from unittest.mock import AsyncMock, patch
from typer.testing import CliRunner
from uuid import uuid4
from memex_cli.documents import app
from memex_common.schemas import DocumentSearchResult, DocumentSnippet

runner = CliRunner()


@pytest.fixture
def mock_api():
    api = AsyncMock()
    return api


def test_cli_doc_search_success(mock_api):
    """Test 'memex doc search' command with valid results."""

    # Create mock results using Pydantic models
    doc_id = uuid4()
    snippet_id = uuid4()

    result = DocumentSearchResult(
        document_id=doc_id,
        metadata={'title': 'Test Document', 'filename': 'test.md'},
        snippets=[
            DocumentSnippet(
                id=snippet_id, text='This is a test snippet.', event_date='2023-01-01T12:00:00'
            )
        ],
        score=0.95,
    )

    mock_api.search_documents.return_value = [result]

    # Mock get_api_context to yield our mock api
    with patch('memex_cli.documents.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'])

        assert result.exit_code == 0
        assert 'Test Document' in result.stdout
        assert str(doc_id) in result.stdout
        assert 'This is a' in result.stdout
        assert 'test snippet' in result.stdout


def test_cli_doc_search_empty(mock_api):
    """Test 'memex doc search' command with no results."""

    mock_api.search_documents.return_value = []

    with patch('memex_cli.documents.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'])

        assert result.exit_code == 0
        assert 'No documents found' in result.stdout


def test_cli_doc_search_error(mock_api):
    """Test 'memex doc search' command handling API errors."""

    mock_api.search_documents.side_effect = Exception('API Error')

    with patch('memex_cli.documents.get_api_context') as mock_ctx:
        mock_ctx.return_value.__aenter__.return_value = mock_api

        result = runner.invoke(app, ['search', 'test query'])

        assert result.exit_code == 1
        assert 'Error: API Error' in result.stdout
