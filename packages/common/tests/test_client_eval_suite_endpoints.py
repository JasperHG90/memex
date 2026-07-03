"""Tests for the new RemoteMemexAPI client methods used by the eval suite.

- ``list_memory_units_by_note(note_id, vault_id)`` calls
  ``GET /notes/{note_id}/memory_units`` with ``vault_id`` as a query param.
- ``get_system_config()`` calls ``GET /system/config`` and returns the
  raw redacted dict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


@pytest.fixture
def mock_client() -> AsyncMock:
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.mark.asyncio
async def test_list_memory_units_by_note_calls_correct_endpoint(mock_client: AsyncMock) -> None:
    api = RemoteMemexAPI(mock_client)
    captured: dict = {}

    async def _get(path, params=None, **kwargs):
        captured['path'] = path
        captured['params'] = params
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = []
        response.raise_for_status = MagicMock()
        return response

    mock_client.get = _get

    note_id = '11111111-1111-1111-1111-111111111111'
    vault_id = '22222222-2222-2222-2222-222222222222'
    result = await api.list_memory_units_by_note(note_id, vault_id)

    assert result == []
    assert captured['path'] == f'notes/{note_id}/memory_units'
    assert captured['params'] == {'vault_id': vault_id, 'include_vectors': False}


@pytest.mark.asyncio
async def test_list_memory_units_by_note_parses_dtos(mock_client: AsyncMock) -> None:
    api = RemoteMemexAPI(mock_client)
    sample_unit = {
        'id': '33333333-3333-3333-3333-333333333333',
        'note_id': '11111111-1111-1111-1111-111111111111',
        'vault_id': '22222222-2222-2222-2222-222222222222',
        'chunk_id': '44444444-4444-4444-4444-444444444444',
        'text': 'Sarah Chen leads Project Alpha.',
        'fact_type': 'world',
    }

    async def _get(path, params=None, **kwargs):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = [sample_unit]
        response.raise_for_status = MagicMock()
        return response

    mock_client.get = _get

    result = await api.list_memory_units_by_note(
        '11111111-1111-1111-1111-111111111111',
        '22222222-2222-2222-2222-222222222222',
    )
    assert len(result) == 1
    assert str(result[0].id) == '33333333-3333-3333-3333-333333333333'


@pytest.mark.asyncio
async def test_get_system_config_calls_correct_endpoint(mock_client: AsyncMock) -> None:
    api = RemoteMemexAPI(mock_client)
    captured: dict = {}

    async def _get(path, params=None, **kwargs):
        captured['path'] = path
        captured['params'] = params
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = {
            'server': {'memory': {'retrieval': {'reranking_mw_alpha': 0.3}}},
            'auth': {'webhook_secret': '<redacted>', 'webhook_secret_set': False},
        }
        response.raise_for_status = MagicMock()
        return response

    mock_client.get = _get

    result = await api.get_system_config()

    assert captured['path'] == 'system/config'
    assert captured['params'] is None
    assert result['server']['memory']['retrieval']['reranking_mw_alpha'] == 0.3
    assert result['auth']['webhook_secret'] == '<redacted>'
