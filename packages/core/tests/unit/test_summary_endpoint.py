"""Tests for the /recall/summary endpoint."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from memex_core.server import app
from memex_core.server.common import get_api
from types import SimpleNamespace


@pytest.fixture
def mock_api():
    """Provides a mocked MemexAPI instance for endpoint tests."""
    api_mock = AsyncMock()
    api_mock.config = SimpleNamespace(server=SimpleNamespace(active_vault='default-vault'))
    api_mock.summarize_search_results.return_value = 'Summary with [0] citation.'
    return api_mock


@pytest.fixture
def client(mock_api):
    """Overrides the API dependency and returns a TestClient."""
    app.dependency_overrides[get_api] = lambda: mock_api
    yield TestClient(app)
    app.dependency_overrides = {}


def test_summary_endpoint_success(client, mock_api):
    """Verify /recall/summary returns 200 with valid payload."""
    payload = {
        'query': 'What is memex?',
        'texts': ['Memex is a memory system.', 'It stores notes.'],
    }

    response = client.post('/api/v1/recall/summary', json=payload)

    assert response.status_code == 200, f'Response: {response.text}'
    data = response.json()
    assert data['summary'] == 'Summary with [0] citation.'

    mock_api.summarize_search_results.assert_called_once_with(
        query='What is memex?',
        texts=['Memex is a memory system.', 'It stores notes.'],
    )


def test_summary_endpoint_validation_error(client):
    """Verify /recall/summary returns 422 for missing required fields."""
    # Missing 'texts' field
    response = client.post('/api/v1/recall/summary', json={'query': 'test'})
    assert response.status_code == 422

    # Missing 'query' field
    response = client.post('/api/v1/recall/summary', json={'texts': ['a']})
    assert response.status_code == 422

    # Empty body
    response = client.post('/api/v1/recall/summary', json={})
    assert response.status_code == 422


def test_summary_endpoint_server_error(client, mock_api):
    """Verify /recall/summary returns 500 when API raises an exception."""
    mock_api.summarize_search_results.side_effect = RuntimeError('LLM unavailable')

    payload = {
        'query': 'test',
        'texts': ['text'],
    }

    response = client.post('/api/v1/recall/summary', json=payload)
    assert response.status_code == 500
