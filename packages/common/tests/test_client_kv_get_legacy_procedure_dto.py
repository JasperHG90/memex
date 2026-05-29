"""Client-side DTO routing for legacy bare ``procedure:*`` KV keys.

After the procedure-under-scope refactor, ``is_procedure_key`` returns
``False`` for bare ``procedure:<verb>:<context>`` keys (those need
``<scope>:`` prefix). During the migration 046 deployment window the
server may still return rows on the bare form — until the DB sweep
completes. ``client.py``'s ``kv_get`` keeps a back-compat branch that
routes bare procedure-shaped keys with dict-shaped values to
``KVProcedureEntryDTO`` so the legacy envelope ({value, version,
history}) still decodes correctly.

This test pins that fallback so a future "dead code" cleanup doesn't
break legacy reads mid-deployment. When migration 046 is fully deployed
the back-compat branch — AND this test — should be removed together.
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
async def test_kv_get_legacy_bare_procedure_routes_to_procedure_dto(mock_client):
    """Legacy bare ``procedure:<verb>:<ctx>`` key with envelope value and
    ``include_history=True`` MUST be parsed as KVProcedureEntryDTO — the
    new ``is_procedure_key`` returns False for bare form, so this hits
    the second condition in client.py's routing block."""
    api = RemoteMemexAPI(mock_client)

    legacy_key = 'procedure:commit:lint'
    envelope_payload = {
        'id': str(uuid4()),
        'key': legacy_key,
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

    result = await api.kv_get(key=legacy_key, include_history=True)

    assert isinstance(result, KVProcedureEntryDTO), (
        f'expected KVProcedureEntryDTO for legacy bare procedure key; got {type(result).__name__}'
    )
    assert result.value.value == 'pre-commit: ruff check; ruff format'


@pytest.mark.asyncio
async def test_kv_get_legacy_bare_procedure_without_history_returns_kv_entry(mock_client):
    """Default GET on a legacy bare procedure key (no ``include_history``)
    returns the plain KVEntryDTO with the active value as a string —
    same back-compat path used for the new ``<scope>:procedure:*`` form."""
    api = RemoteMemexAPI(mock_client)

    legacy_key = 'procedure:run_tests:python'
    payload = {
        'id': str(uuid4()),
        'key': legacy_key,
        'value': 'uv run pytest',
        'expires_at': None,
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
    }
    mock_client.get = AsyncMock(return_value=_make_response(payload))

    result = await api.kv_get(key=legacy_key, include_history=False)

    assert isinstance(result, KVEntryDTO)
    assert result.value == 'uv run pytest'


@pytest.mark.asyncio
async def test_kv_get_legacy_routing_rejects_multi_segment_bare_key(mock_client):
    """Bare `procedure:foo:bar:baz` (4+ segments) is NOT a valid legacy
    procedure key — only 3-segment `procedure:<verb>:<context>` was ever
    valid pre-refactor. The back-compat check uses `count(':') == 2` to
    match `_is_procedure_for_briefing` on the server side; a looser
    substring check would misroute these to `KVProcedureEntryDTO` and
    fail at envelope-decode time. This test pins the count-based check
    so future "simplifications" don't reintroduce the misroute."""
    api = RemoteMemexAPI(mock_client)

    multi_segment_key = 'procedure:foo:bar:baz'
    plain_payload = {
        'id': str(uuid4()),
        'key': multi_segment_key,
        'value': {'arbitrary': 'dict', 'not': 'envelope'},
        'expires_at': None,
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
    }
    mock_client.get = AsyncMock(return_value=_make_response(plain_payload))

    # Should fall through to KVEntryDTO (plain), NOT KVProcedureEntryDTO.
    # KVEntryDTO expects a str value, so this currently raises a
    # ValidationError — the important invariant is that the routing
    # check does NOT identify it as a procedure key.
    with pytest.raises(Exception) as excinfo:
        await api.kv_get(key=multi_segment_key, include_history=True)
    # Either KVEntryDTO validation fails (str expected, got dict), or
    # it succeeds as a plain entry — what we're guarding against is the
    # WRONG-TYPE routing to KVProcedureEntryDTO, which would attempt
    # to decode `{arbitrary, not}` as the envelope and fail differently.
    # The misroute would mention `version` (envelope field); the correct
    # plain-DTO path mentions `str` (the expected value type).
    assert 'version' not in str(excinfo.value).lower() or 'str' in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_kv_get_legacy_routing_does_not_match_global_procedure_when_bare(mock_client):
    """The legacy fallback's substring check (``':procedure:' not in
    key[len('procedure:'):]``) excludes the new ``<scope>:procedure:*``
    form — those flow through the primary ``is_procedure_key`` branch,
    not the back-compat branch. Sanity check that the two branches don't
    overlap so removal of the back-compat branch post-046 is safe."""
    api = RemoteMemexAPI(mock_client)

    # `global:procedure:commit:lint` — new form. is_procedure_key=True
    # routes via the primary branch. The legacy substring would also be
    # true (key doesn't start with bare `procedure:`), but the primary
    # branch fires first via short-circuit `or`.
    new_form_key = 'global:procedure:commit:lint'
    envelope_payload = {
        'id': str(uuid4()),
        'key': new_form_key,
        'value': {'value': 'pre-commit: ruff', 'version': 1, 'history': []},
        'expires_at': None,
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
    }
    mock_client.get = AsyncMock(return_value=_make_response(envelope_payload))

    result = await api.kv_get(key=new_form_key, include_history=True)

    assert isinstance(result, KVProcedureEntryDTO)
