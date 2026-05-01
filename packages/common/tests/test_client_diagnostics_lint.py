"""Tests for RemoteMemexAPI.get_diagnostics_lint() — F26 HTTP client wrapper."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


@pytest.fixture
def mock_client():
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.mark.asyncio
async def test_get_diagnostics_lint_path_and_response(mock_client):
    """Client GETs /diagnostics/lint/<vault_id> and returns the JSON unchanged."""
    api = RemoteMemexAPI(mock_client)
    vault_id = uuid4()
    expected_payload = {
        'vault_id': str(vault_id),
        'counts_by_type_status_source': [
            {'lint_type': 'structural', 'status': 'pending', 'source': 'rule', 'count': 2}
        ],
        'pending_by_type': {'structural': 2},
        'top_5_pending': [],
    }

    captured: dict = {}

    async def _capture_get(path, params=None, **kwargs):
        captured['path'] = path
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = expected_payload
        response.raise_for_status = MagicMock()
        return response

    mock_client.get = _capture_get

    result = await api.get_diagnostics_lint(vault_id)

    assert captured['path'] == f'diagnostics/lint/{vault_id}'
    assert result == expected_payload


@pytest.mark.asyncio
async def test_get_diagnostics_lint_accepts_str_vault_id(mock_client):
    """vault_id parameter accepts str (consistent with sibling diagnostics methods)."""
    api = RemoteMemexAPI(mock_client)

    captured: dict = {}

    async def _capture_get(path, params=None, **kwargs):
        captured['path'] = path
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = {'content-type': 'application/json'}
        response.json.return_value = {}
        response.raise_for_status = MagicMock()
        return response

    mock_client.get = _capture_get

    vault_str = '11111111-1111-1111-1111-111111111111'
    await api.get_diagnostics_lint(vault_str)
    assert captured['path'] == f'diagnostics/lint/{vault_str}'
