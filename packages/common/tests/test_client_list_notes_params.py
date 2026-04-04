"""Tests for RemoteMemexAPI.list_notes() tags/status parameter passthrough."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


def _mock_ndjson_response(data: list | None = None):
    """Create a mock NDJSON response (content-type: application/x-ndjson)."""
    import json

    lines = [json.dumps(d) for d in (data or [])]
    body = '\n'.join(lines)

    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {'content-type': 'application/x-ndjson'}
    response.text = body
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_list_notes_passes_tags_param():
    """tags parameter is sent as query params in the GET request."""
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict = {}

    async def capture_get(path, params=None, **kwargs):
        captured['path'] = path
        captured['params'] = params
        return _mock_ndjson_response()

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.list_notes(tags=['python', 'ai'])

    assert captured['params']['tags'] == ['python', 'ai']


@pytest.mark.asyncio
async def test_list_notes_passes_status_param():
    """status parameter is sent as a query param in the GET request."""
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_ndjson_response()

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.list_notes(status='archived')

    assert captured['params']['status'] == 'archived'


@pytest.mark.asyncio
async def test_list_notes_omits_tags_and_status_when_none():
    """tags and status are not included in params when None (backward compat)."""
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_ndjson_response()

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.list_notes()

    assert 'tags' not in captured['params']
    assert 'status' not in captured['params']


@pytest.mark.asyncio
async def test_list_notes_passes_tags_and_status_together():
    """Both tags and status are sent when provided."""
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_ndjson_response()

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.list_notes(tags=['devops'], status='active')

    assert captured['params']['tags'] == ['devops']
    assert captured['params']['status'] == 'active'
