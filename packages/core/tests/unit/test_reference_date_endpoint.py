"""Tests for reference_date threading from REST endpoints to the API layer."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from memex_core.server import app
from memex_core.server.common import get_api
from uuid import UUID
from datetime import datetime, timezone
from types import SimpleNamespace
from memex_common.types import FactTypes

MOCK_VAULT_ID = UUID('00000000-0000-0000-0000-000000000001')
MOCK_UNIT_ID = UUID('00000000-0000-0000-0000-000000000002')


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.config = SimpleNamespace(server=SimpleNamespace(default_active_vault='default-vault'))
    api.resolve_vault_identifier.return_value = MOCK_VAULT_ID
    api.search.return_value = (
        [
            SimpleNamespace(
                id=MOCK_UNIT_ID,
                text='Found memory',
                fact_type=FactTypes.WORLD,
                status='active',
                mentioned_at=datetime.now(timezone.utc),
                event_date=datetime.now(timezone.utc),
                occurred_start=None,
                occurred_end=None,
                vault_id=MOCK_VAULT_ID,
                unit_metadata={},
                score=0.95,
            )
        ],
        None,
    )
    api.search_notes.return_value = []
    return api


@pytest.fixture
def client(mock_api):
    app.dependency_overrides[get_api] = lambda: mock_api
    yield TestClient(app)
    app.dependency_overrides = {}


def test_memory_search_accepts_reference_date(client, mock_api):
    """POST /memories/search should pass reference_date to api.search()."""
    payload = {
        'query': 'what happened last week',
        'reference_date': '2025-06-15T12:00:00Z',
    }
    response = client.post('/api/v1/memories/search', json=payload)
    assert response.status_code == 200

    mock_api.search.assert_called_once()
    kwargs = mock_api.search.call_args.kwargs
    assert kwargs['reference_date'] == datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_memory_search_reference_date_defaults_to_none(client, mock_api):
    """reference_date should be None when not provided in the request body."""
    payload = {'query': 'anything'}
    response = client.post('/api/v1/memories/search', json=payload)
    assert response.status_code == 200

    kwargs = mock_api.search.call_args.kwargs
    assert kwargs['reference_date'] is None


def test_note_search_accepts_reference_date(client, mock_api):
    """POST /notes/search should pass reference_date to api.search_notes()."""
    payload = {
        'query': 'meetings from last month',
        'reference_date': '2025-03-01T00:00:00Z',
    }
    response = client.post('/api/v1/notes/search', json=payload)
    assert response.status_code == 200

    mock_api.search_notes.assert_called_once()
    kwargs = mock_api.search_notes.call_args.kwargs
    assert kwargs['reference_date'] == datetime(2025, 3, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_note_search_reference_date_defaults_to_none(client, mock_api):
    """reference_date should be None when not provided in note search."""
    payload = {'query': 'anything'}
    response = client.post('/api/v1/notes/search', json=payload)
    assert response.status_code == 200

    kwargs = mock_api.search_notes.call_args.kwargs
    assert kwargs.get('reference_date') is None
