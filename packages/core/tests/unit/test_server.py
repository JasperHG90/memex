import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from memex_core.server import app
from memex_core.server.common import get_api
from uuid import UUID
from datetime import datetime, timezone
from types import SimpleNamespace
from memex_common.types import FactTypes

# Mock Data
MOCK_VAULT_ID = UUID('00000000-0000-0000-0000-000000000001')
MOCK_UNIT_ID = UUID('00000000-0000-0000-0000-000000000002')


@pytest.fixture
def mock_api():
    """Provides a mocked MemexAPI instance."""
    api_mock = AsyncMock()

    # Mock Config
    api_mock.config = SimpleNamespace(server=SimpleNamespace(default_active_vault='default-vault'))
    api_mock.resolve_vault_identifier.return_value = MOCK_VAULT_ID

    # Setup default return values for common methods
    api_mock.ingest.return_value = {
        'status': 'success',
        'unit_ids': [MOCK_UNIT_ID],
        'note_id': 'doc_123',
    }

    # Use SimpleNamespace to simulate an object with attributes that Pydantic can read
    api_mock.create_vault.return_value = SimpleNamespace(
        id=MOCK_VAULT_ID, name='Test Vault', description='A test vault'
    )

    api_mock.search.return_value = (
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

    return api_mock


@pytest.fixture
def client(mock_api):
    """Overrides API dependency and returns a TestClient."""
    app.dependency_overrides[get_api] = lambda: mock_api
    yield TestClient(app)
    app.dependency_overrides = {}


def test_ingest_note_validation(client, mock_api):
    """Verify /ingestions accepts JSON body and calls API."""
    import base64

    content = base64.b64encode(b'Test content').decode('utf-8')

    payload = {
        'name': 'Test Note',
        'description': 'Test Desc',
        'content': content,
        'files': {},
        'tags': ['test'],
    }

    response = client.post('/api/v1/ingestions', json=payload)

    assert response.status_code == 200, f'Response: {response.text}'

    # Verify API was called with correct data objects
    mock_api.ingest.assert_called_once()
    call_args = mock_api.ingest.call_args[0][0]

    # Check private metadata since Note doesn't expose public props
    assert call_args._metadata.name == 'Test Note'
    # Tags are added to metadata during init
    assert 'test' in call_args._metadata.tags

    # Verify Response DTO
    data = response.json()
    assert data['status'] == 'success'
    assert data['unit_ids'][0] == str(MOCK_UNIT_ID)


def test_create_vault_validation(client, mock_api):
    """Verify /vaults accepts JSON body."""
    payload = {'name': 'New Vault', 'description': 'Secure storage'}

    response = client.post('/api/v1/vaults', json=payload)

    assert response.status_code == 200, f'Response: {response.text}'
    mock_api.create_vault.assert_called_once_with(name='New Vault', description='Secure storage')

    data = response.json()
    assert data['id'] == str(MOCK_VAULT_ID)


def test_retrieve_validation(client, mock_api):
    """Verify /memories/search accepts JSON body."""
    payload = {'query': 'something', 'limit': 5}

    response = client.post('/api/v1/memories/search', json=payload)

    assert response.status_code == 200, f'Response: {response.text}'

    mock_api.search.assert_called_once()

    import json

    data = [json.loads(line) for line in response.text.strip().split('\n') if line]
    assert len(data) == 1
    assert data[0]['text'] == 'Found memory'


def test_claim_reflection_queue(client, mock_api):
    """Verify /reflections/claim calls API and returns DTOs."""
    mock_api.claim_reflection_queue_batch.return_value = [
        SimpleNamespace(entity_id=MOCK_UNIT_ID, vault_id=MOCK_VAULT_ID, priority_score=0.88)
    ]

    response = client.post('/api/v1/reflections/claim?limit=5')

    assert response.status_code == 200
    mock_api.claim_reflection_queue_batch.assert_called_once_with(limit=5)

    import json

    data = [json.loads(line) for line in response.text.strip().split('\n') if line]
    assert len(data) == 1
    assert data[0]['entity_id'] == str(MOCK_UNIT_ID)
    assert data[0]['priority_score'] == 0.88


def test_vault_not_found_error_handling(client, mock_api):
    """Verify that VaultNotFoundError maps to 404."""
    from memex_common.exceptions import VaultNotFoundError

    mock_api.ingest.side_effect = VaultNotFoundError("Vault 'missing' not found")

    payload = {
        'name': 'Test Note',
        'description': 'Test Desc',
        'content': 'dGVzdA==',  # base64 for "test"
        'files': {},
        'tags': [],
    }

    response = client.post('/api/v1/ingestions', json=payload)

    assert response.status_code == 404
    assert "Vault 'missing' not found" in response.json()['detail']


def test_metrics_endpoint(client):
    """Verify that /metrics endpoint is exposed and returns 200."""
    response = client.get('/api/v1/metrics')
    assert response.status_code == 200
    # Check for standard Prometheus output
    assert '# HELP' in response.text
    assert '# TYPE' in response.text


def test_get_note_page_index_with_data(client, mock_api):
    """GET /notes/{document_id}/page-index returns page_index when present."""
    doc_id = UUID('00000000-0000-0000-0000-000000000099')
    page_index = {'toc': [{'level': 1, 'title': 'Intro', 'children': []}]}
    mock_api.get_note_page_index.return_value = page_index

    response = client.get(f'/api/v1/notes/{doc_id}/page-index')

    assert response.status_code == 200
    data = response.json()
    assert data['note_id'] == str(doc_id)
    assert data['page_index'] == page_index
    mock_api.get_note_page_index.assert_called_once_with(doc_id)


def test_get_note_page_index_none(client, mock_api):
    """GET /notes/{document_id}/page-index returns null page_index when note has none."""
    doc_id = UUID('00000000-0000-0000-0000-000000000098')
    mock_api.get_note_page_index.return_value = None

    response = client.get(f'/api/v1/notes/{doc_id}/page-index')

    assert response.status_code == 200
    data = response.json()
    assert data['note_id'] == str(doc_id)
    assert data['page_index'] is None


def test_get_note_page_index_not_found(client, mock_api):
    """GET /notes/{document_id}/page-index returns 404 for missing notes."""
    from memex_common.exceptions import ResourceNotFoundError

    doc_id = UUID('00000000-0000-0000-0000-000000000097')
    mock_api.get_note_page_index.side_effect = ResourceNotFoundError('Not found')

    response = client.get(f'/api/v1/notes/{doc_id}/page-index')

    assert response.status_code == 404


def test_get_note_metadata_with_data(client, mock_api):
    """GET /notes/{note_id}/metadata returns metadata when present."""
    doc_id = UUID('00000000-0000-0000-0000-000000000099')
    metadata = {'title': 'Test', 'description': 'A note', 'tags': ['a', 'b']}
    mock_api.get_note_metadata.return_value = metadata

    response = client.get(f'/api/v1/notes/{doc_id}/metadata')

    assert response.status_code == 200
    data = response.json()
    assert data['note_id'] == str(doc_id)
    assert data['metadata'] == metadata
    mock_api.get_note_metadata.assert_called_once_with(doc_id)


def test_get_note_metadata_none(client, mock_api):
    """GET /notes/{note_id}/metadata returns null when note has no page index."""
    doc_id = UUID('00000000-0000-0000-0000-000000000098')
    mock_api.get_note_metadata.return_value = None

    response = client.get(f'/api/v1/notes/{doc_id}/metadata')

    assert response.status_code == 200
    data = response.json()
    assert data['note_id'] == str(doc_id)
    assert data['metadata'] is None


def test_get_note_metadata_not_found(client, mock_api):
    """GET /notes/{note_id}/metadata returns 404 for missing notes."""
    from memex_common.exceptions import ResourceNotFoundError

    doc_id = UUID('00000000-0000-0000-0000-000000000097')
    mock_api.get_note_metadata.side_effect = ResourceNotFoundError('Not found')

    response = client.get(f'/api/v1/notes/{doc_id}/metadata')

    assert response.status_code == 404
