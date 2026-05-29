"""Client-side DTO routing for procedure KV keys.

After the procedure-under-scope refactor, ``is_procedure_key`` returns
``True`` only for ``<scope>:procedure:<verb>:<context>`` keys (those
carrying a ``global:``/``user:``/``project:<id>:``/``app:<id>:`` prefix)
and ``False`` for the legacy bare ``procedure:<verb>:<context>`` form.

``client.py``'s ``kv_get`` routes only valid scoped procedure keys with
dict-shaped values to ``KVProcedureEntryDTO``. The back-compat branch
that used to route legacy bare keys was removed once migration 046 was
deployed; these tests pin the post-refactor behaviour so the bare form
is treated as an ordinary KV key, not a procedure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import KVEntryDTO, KVProcedureEntryDTO


@pytest.fixture
def mock_client():
    return AsyncMock(spec=httpx.AsyncClient)


def _make_response(payload: dict) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {'content-type': 'application/json'}
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_kv_get_bare_procedure_does_not_route_to_procedure_dto(mock_client):
    """A bare ``procedure:<verb>:<ctx>`` key is NOT a valid procedure key
    post-refactor — ``is_procedure_key`` returns False, so it must fall
    through to the plain ``KVEntryDTO`` branch. With a dict-shaped value
    (the legacy envelope) that branch raises a validation error rather
    than silently decoding it as a procedure envelope."""
    api = RemoteMemexAPI(mock_client)

    bare_key = 'procedure:commit:lint'
    envelope_payload = {
        'id': str(uuid4()),
        'key': bare_key,
        'value': {
            'value': 'pre-commit: ruff check; ruff format',
            'version': 1,
            'history': [],
        },
        'expires_at': None,
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
    }
    mock_client.get = AsyncMock(return_value=_make_response(envelope_payload))

    with pytest.raises(Exception):
        await api.kv_get(key=bare_key, include_history=True)


@pytest.mark.asyncio
async def test_kv_get_bare_procedure_without_history_returns_kv_entry(mock_client):
    """Default GET on a bare procedure-shaped key (no ``include_history``)
    returns a plain KVEntryDTO with the active value as a string — it is
    treated as an ordinary KV key, never a procedure."""
    api = RemoteMemexAPI(mock_client)

    bare_key = 'procedure:run_tests:python'
    payload = {
        'id': str(uuid4()),
        'key': bare_key,
        'value': 'uv run pytest',
        'expires_at': None,
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
    }
    mock_client.get = AsyncMock(return_value=_make_response(payload))

    result = await api.kv_get(key=bare_key, include_history=False)

    assert isinstance(result, KVEntryDTO)
    assert result.value == 'uv run pytest'


@pytest.mark.asyncio
async def test_kv_get_scoped_procedure_routes_to_procedure_dto(mock_client):
    """A valid ``<scope>:procedure:*`` key with an envelope value and
    ``include_history=True`` routes to KVProcedureEntryDTO via the
    ``is_procedure_key`` branch."""
    api = RemoteMemexAPI(mock_client)

    scoped_key = 'global:procedure:commit:lint'
    envelope_payload = {
        'id': str(uuid4()),
        'key': scoped_key,
        'value': {'value': 'pre-commit: ruff', 'version': 1, 'history': []},
        'expires_at': None,
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
    }
    mock_client.get = AsyncMock(return_value=_make_response(envelope_payload))

    result = await api.kv_get(key=scoped_key, include_history=True)

    assert isinstance(result, KVProcedureEntryDTO)
    assert result.value.value == 'pre-commit: ruff'
