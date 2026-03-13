import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock
from memex_core.server import app
from memex_core.server.common import get_api

# Module-level mock — reset before each test via the fixture below.
mock_api = MagicMock()


@pytest.fixture(autouse=True)
def _reset_mock_api():
    """Reset mock_api call state before each test to prevent cross-test leakage."""
    mock_api.reset_mock()


@pytest.fixture
def client():
    app.dependency_overrides[get_api] = lambda: mock_api
    yield TestClient(app)
    app.dependency_overrides.pop(get_api, None)


def test_ingest_upload_single_md(client):
    mock_api.ingest = AsyncMock(return_value={'status': 'success', 'note_id': '123'})

    files = [('files', ('test.md', b'# Test Content', 'text/markdown'))]

    response = client.post('/api/v1/ingestions/upload', files=files)

    assert response.status_code == 200
    assert response.json()['status'] == 'success'
    assert mock_api.ingest.called


def test_ingest_upload_single_non_md(client):
    mock_api.ingest_from_file = AsyncMock(return_value={'status': 'success', 'note_id': '456'})

    files = [('files', ('test.pdf', b'pdf data', 'application/pdf'))]

    response = client.post('/api/v1/ingestions/upload', files=files)

    assert response.status_code == 200
    assert response.json()['status'] == 'success'
    assert mock_api.ingest_from_file.called


def test_ingest_upload_directory_style(client):
    mock_api.ingest = AsyncMock(return_value={'status': 'success', 'note_id': '789'})

    files = [
        ('files', ('NOTE.md', b'# Main Note', 'text/markdown')),
        ('files', ('image.png', b'png data', 'image/png')),
    ]

    response = client.post('/api/v1/ingestions/upload', files=files)

    assert response.status_code == 200
    assert response.json()['status'] == 'success'
    assert mock_api.ingest.called

    # Check that it correctly identified main content and aux files
    note = mock_api.ingest.call_args[0][0]
    assert note._content == b'# Main Note'
    assert 'image.png' in note._files
