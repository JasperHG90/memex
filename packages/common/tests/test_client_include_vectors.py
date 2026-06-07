"""RemoteMemexAPI ``include_vectors`` threading + the by-ids batch method.

Covers the client-side short-circuit on an empty ``unit_ids`` list, the
request shapes for the new ``memories/by-ids`` endpoint, and the
``include_vectors`` wiring on every read method that exposes it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


@pytest.fixture
def mock_client():
    return AsyncMock(spec=httpx.AsyncClient)


def _json_response(payload):
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {'content-type': 'application/json'}
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response


def _capture_post(captured: dict, payload=None):
    async def _post(path, json=None, **kwargs):
        captured['count'] = captured.get('count', 0) + 1
        captured['path'] = path
        captured['json'] = json
        return _json_response(payload if payload is not None else [])

    return _post


def _capture_get(captured: dict, payload=None):
    async def _get(path, params=None, **kwargs):
        captured['path'] = path
        captured['params'] = params
        return _json_response(payload if payload is not None else [])

    return _get


class TestGetMemoryUnitsByIds:
    @pytest.mark.asyncio
    async def test_empty_unit_ids_short_circuits_no_request(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        mock_client.post = _capture_post(captured)

        result = await api.get_memory_units_by_ids([], vault_id='vault-uuid')

        assert result == []
        assert captured.get('count', 0) == 0, 'empty unit_ids must not trigger a HTTP POST'

    @pytest.mark.asyncio
    async def test_posts_to_by_ids_with_serialized_body(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        mock_client.post = _capture_post(captured)

        await api.get_memory_units_by_ids(
            ['11111111-1111-1111-1111-111111111111'],
            vault_id='22222222-2222-2222-2222-222222222222',
        )

        assert captured['path'] == 'memories/by-ids'
        assert captured['json']['unit_ids'] == ['11111111-1111-1111-1111-111111111111']
        assert captured['json']['vault_id'] == '22222222-2222-2222-2222-222222222222'
        assert captured['json']['include_vectors'] is False

    @pytest.mark.asyncio
    async def test_include_vectors_true_in_body(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        mock_client.post = _capture_post(captured)

        await api.get_memory_units_by_ids(
            ['11111111-1111-1111-1111-111111111111'],
            vault_id='22222222-2222-2222-2222-222222222222',
            include_vectors=True,
        )

        assert captured['json']['include_vectors'] is True


class TestIncludeVectorsParamThreading:
    @pytest.mark.asyncio
    async def test_get_memory_unit_does_not_send_include_vectors(self, mock_client) -> None:
        """The unscoped single-ID GET never exposes vectors — no flag on the wire."""
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        unit = {
            'id': '11111111-1111-1111-1111-111111111111',
            'text': 't',
            'fact_type': 'world',
            'status': 'active',
        }
        mock_client.get = _capture_get(captured, payload=unit)

        await api.get_memory_unit('11111111-1111-1111-1111-111111111111')

        assert 'include_vectors' not in (captured['params'] or {})

    @pytest.mark.asyncio
    async def test_by_chunks_body_carries_include_vectors(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        mock_client.post = _capture_post(captured)

        await api.get_memory_units_by_chunks(
            ['11111111-1111-1111-1111-111111111111'],
            vault_id='22222222-2222-2222-2222-222222222222',
            include_vectors=True,
        )

        assert captured['json']['include_vectors'] is True

    @pytest.mark.asyncio
    async def test_list_by_note_passes_query_param(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        mock_client.get = _capture_get(captured)

        await api.list_memory_units_by_note(
            '11111111-1111-1111-1111-111111111111',
            vault_id='22222222-2222-2222-2222-222222222222',
            include_vectors=True,
        )

        assert captured['params']['include_vectors'] is True

    @pytest.mark.asyncio
    async def test_get_vault_summary_passes_query_param(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        summary = {
            'id': '11111111-1111-1111-1111-111111111111',
            'vault_id': '22222222-2222-2222-2222-222222222222',
            'narrative': 'n',
            'themes': [],
            'inventory': {},
            'key_entities': [],
            'version': 1,
            'notes_incorporated': 0,
            'created_at': '2026-01-01T00:00:00Z',
            'updated_at': '2026-01-01T00:00:00Z',
        }
        mock_client.get = _capture_get(captured, payload=summary)

        await api.get_vault_summary('22222222-2222-2222-2222-222222222222', include_vectors=True)

        assert captured['params']['include_vectors'] is True

    @pytest.mark.asyncio
    async def test_kv_get_passes_query_param(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        entry = {
            'id': '11111111-1111-1111-1111-111111111111',
            'key': 'user:editor',
            'value': 'neovim',
            'created_at': '2026-01-01T00:00:00Z',
            'updated_at': '2026-01-01T00:00:00Z',
        }
        mock_client.get = _capture_get(captured, payload=entry)

        await api.kv_get('user:editor', include_vectors=True)

        assert captured['params']['include_vectors'] is True

    @pytest.mark.asyncio
    async def test_kv_list_passes_query_param(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}
        mock_client.get = _capture_get(captured)

        await api.kv_list(include_vectors=True)

        assert captured['params']['include_vectors'] is True

    @pytest.mark.asyncio
    async def test_kv_search_body_carries_include_vectors(self, mock_client) -> None:
        api = RemoteMemexAPI(mock_client)
        captured: dict = {}

        async def _post(path, json=None, content=None, **kwargs):
            captured['path'] = path
            captured['json'] = json
            captured['content'] = content
            return _json_response([])

        mock_client.post = _post

        await api.kv_search([0.1, 0.2], include_vectors=True)

        body = captured['json']
        if body is None and captured.get('content') is not None:
            import json as _json

            body = _json.loads(captured['content'])
        assert body['include_vectors'] is True
