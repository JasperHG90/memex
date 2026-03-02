"""Tests for RemoteMemexAPI.search() parameter passthrough."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


@pytest.fixture
def mock_client():
    """Create a mock httpx.AsyncClient that captures requests."""
    client = AsyncMock(spec=httpx.AsyncClient)

    async def mock_post(path, json=None, **kwargs):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = []
        response.raise_for_status = MagicMock()
        # Store the request for assertion
        response._request_json = json
        return response

    client.post = mock_post
    return client


@pytest.fixture
def api(mock_client):
    return RemoteMemexAPI(mock_client)


@pytest.mark.asyncio
async def test_search_passes_include_stale(mock_client):
    """include_stale=True should be passed through to the RetrievalRequest."""
    api = RemoteMemexAPI(mock_client)

    # Capture the actual post call
    captured = {}

    async def capture_post(path, json=None, **kwargs):
        captured['path'] = path
        captured['json'] = json
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = []
        response.raise_for_status = MagicMock()
        return response

    mock_client.post = capture_post

    await api.search(query='test', include_stale=True)

    assert captured['json']['include_stale'] is True


@pytest.mark.asyncio
async def test_search_include_stale_default_false(mock_client):
    """include_stale should default to False."""
    api = RemoteMemexAPI(mock_client)

    captured = {}

    async def capture_post(path, json=None, **kwargs):
        captured['json'] = json
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = []
        response.raise_for_status = MagicMock()
        return response

    mock_client.post = capture_post

    await api.search(query='test')

    assert captured['json']['include_stale'] is False


@pytest.mark.asyncio
async def test_search_passes_vault_ids(mock_client):
    """vault_ids should be passed through to the RetrievalRequest."""
    api = RemoteMemexAPI(mock_client)

    captured = {}

    async def capture_post(path, json=None, **kwargs):
        captured['json'] = json
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = []
        response.raise_for_status = MagicMock()
        return response

    mock_client.post = capture_post

    vault_name = 'my-vault'
    await api.search(query='test', vault_ids=[vault_name])

    assert captured['json']['vault_ids'] == [vault_name]


@pytest.mark.asyncio
async def test_search_passes_strategies(mock_client):
    """strategies list should be passed through to the RetrievalRequest."""
    api = RemoteMemexAPI(mock_client)

    captured = {}

    async def capture_post(path, json=None, **kwargs):
        captured['json'] = json
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = []
        response.raise_for_status = MagicMock()
        return response

    mock_client.post = capture_post

    await api.search(query='test', strategies=['semantic', 'keyword'])

    assert captured['json']['strategies'] == ['semantic', 'keyword']
