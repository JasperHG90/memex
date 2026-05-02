"""Tests for RemoteMemexAPI.get_memory_units_by_chunks() — F46 HTTP wrapper.

Covers the client-side short-circuit on an empty ``chunk_ids`` list (mirrors
the service-layer guard in ``StatsService.get_memory_units_by_chunks``) so an
empty batch never costs a network round-trip.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


@pytest.fixture
def mock_client():
    """httpx.AsyncClient stub that records whether POST was called."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.mark.asyncio
async def test_empty_chunk_ids_short_circuits_no_request(mock_client):
    """Empty chunk_ids list returns [] without hitting the wire (MED-1)."""
    api = RemoteMemexAPI(mock_client)
    posted: dict = {'count': 0}

    async def _post(path, json=None, **kwargs):
        posted['count'] += 1
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = []
        response.raise_for_status = MagicMock()
        return response

    mock_client.post = _post

    result = await api.get_memory_units_by_chunks([], vault_id='vault-uuid')

    assert result == []
    assert posted['count'] == 0, 'empty chunk_ids must not trigger a HTTP POST'


@pytest.mark.asyncio
async def test_non_empty_chunk_ids_posts_to_by_chunks(mock_client):
    """Non-empty chunk_ids forwards to /memories/by-chunks with serialized body."""
    api = RemoteMemexAPI(mock_client)
    captured: dict = {}

    async def _post(path, json=None, **kwargs):
        captured['path'] = path
        captured['json'] = json
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = []
        response.raise_for_status = MagicMock()
        return response

    mock_client.post = _post

    await api.get_memory_units_by_chunks(
        ['11111111-1111-1111-1111-111111111111'],
        vault_id='22222222-2222-2222-2222-222222222222',
    )

    assert captured['path'] == 'memories/by-chunks'
    assert captured['json']['chunk_ids'] == ['11111111-1111-1111-1111-111111111111']
    assert captured['json']['vault_id'] == '22222222-2222-2222-2222-222222222222'
