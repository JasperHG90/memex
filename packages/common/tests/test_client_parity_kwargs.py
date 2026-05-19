"""RemoteMemexAPI ↔ MemexAPI kwarg parity tests.

Each test pins that a kwarg the local ``MemexAPI`` accepts is actually
forwarded by the HTTP client wrapper. Drift between the two surfaces is
silent at type-check time because the MCP/Hermes layer goes through a
``MemexAPIProtocol`` with ``**kwargs: Any`` — the only way to catch
the gap is to exercise the wrapper.

See :mod:`packages.common.tests.test_client_signature_parity` for the
catch-all signature audit; this file pins behaviour (the kwarg lands on
the wire).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from memex_common.client import RemoteMemexAPI


# ---------------------------------------------------------------------------
# Test harness — captures the request shape the wrapper produces.
# ---------------------------------------------------------------------------


def _mock_json_response(payload: Any) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {'content-type': 'application/json'}
    response.json = MagicMock(return_value=payload)
    response.raise_for_status = MagicMock()
    return response


def _mock_ndjson_response(items: list[dict[str, Any]] | None = None) -> MagicMock:
    import json

    body = '\n'.join(json.dumps(d) for d in (items or []))
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {'content-type': 'application/x-ndjson'}
    response.text = body
    response.raise_for_status = MagicMock()
    return response


def _make_client_capturing(
    *,
    get_response: Any = None,
    post_response: Any = None,
) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    """Build an AsyncMock client whose GET/POST calls land in a shared dict."""
    captured: dict[str, Any] = {}
    client = AsyncMock(spec=httpx.AsyncClient)

    async def capture_get(path, params=None, **kwargs):
        captured.setdefault('gets', []).append({'path': path, 'params': params})
        if get_response is None:
            return _mock_ndjson_response()
        return get_response

    async def capture_post(path, json=None, params=None, **kwargs):
        captured.setdefault('posts', []).append({'path': path, 'json': json, 'params': params})
        if post_response is None:
            return _mock_json_response([])
        return post_response

    client.get = capture_get
    client.post = capture_post
    return client, captured


# ---------------------------------------------------------------------------
# search.debug
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_forwards_debug_when_true() -> None:
    client, captured = _make_client_capturing(post_response=_mock_json_response([]))
    api = RemoteMemexAPI(client)

    await api.search('query', debug=True)

    body = captured['posts'][0]['json']
    assert body['debug'] is True


@pytest.mark.asyncio
async def test_search_omits_debug_by_default() -> None:
    client, captured = _make_client_capturing(post_response=_mock_json_response([]))
    api = RemoteMemexAPI(client)

    await api.search('query')

    body = captured['posts'][0]['json']
    # ``debug`` is a declared field on RetrievalRequest with default False;
    # the request body should carry it explicitly as the default.
    assert body.get('debug') is False


# ---------------------------------------------------------------------------
# search_notes.mmr_lambda
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_notes_forwards_mmr_lambda() -> None:
    client, captured = _make_client_capturing(post_response=_mock_json_response([]))
    api = RemoteMemexAPI(client)

    await api.search_notes('query', mmr_lambda=0.7)

    body = captured['posts'][0]['json']
    assert body['mmr_lambda'] == 0.7


@pytest.mark.asyncio
async def test_search_notes_mmr_lambda_omitted_when_none() -> None:
    """None → field present on the request as None (NoteSearchRequest default)."""
    client, captured = _make_client_capturing(post_response=_mock_json_response([]))
    api = RemoteMemexAPI(client)

    await api.search_notes('query')

    body = captured['posts'][0]['json']
    # NoteSearchRequest declares mmr_lambda: float | None = None — Pydantic
    # serializes it as null.
    assert body.get('mmr_lambda') is None


# ---------------------------------------------------------------------------
# find_notes_by_title.threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_notes_by_title_forwards_threshold() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.find_notes_by_title('jwt', threshold=0.5)

    assert captured['params']['threshold'] == 0.5


@pytest.mark.asyncio
async def test_find_notes_by_title_threshold_defaults_to_engine_default() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.find_notes_by_title('jwt')

    assert captured['params']['threshold'] == 0.3  # MemexAPI default


# ---------------------------------------------------------------------------
# get_entity / get_entities .vault_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_forwards_vault_id_query_param() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['path'] = path
        captured['params'] = params
        return _mock_json_response({'id': str(uuid4()), 'name': 'X', 'mention_count': 0})

    client.get = capture_get
    api = RemoteMemexAPI(client)

    entity_id = uuid4()
    vault_id = uuid4()
    await api.get_entity(entity_id, vault_id=vault_id)

    assert captured['path'] == f'entities/{entity_id}'
    assert captured['params']['vault_id'] == str(vault_id)


@pytest.mark.asyncio
async def test_get_entity_omits_vault_id_when_none() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_json_response({'id': str(uuid4()), 'name': 'X', 'mention_count': 0})

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.get_entity(uuid4())

    # No vault_id in params → server falls back to default_active_vault.
    assert captured['params'] is None


@pytest.mark.asyncio
async def test_get_entities_forwards_vault_id_query_param() -> None:
    client, captured = _make_client_capturing(post_response=_mock_json_response([]))
    api = RemoteMemexAPI(client)

    vault_id = uuid4()
    await api.get_entities([uuid4(), uuid4()], vault_id=vault_id)

    post = captured['posts'][0]
    assert post['path'] == 'entities/batch'
    assert post['params']['vault_id'] == str(vault_id)


# ---------------------------------------------------------------------------
# get_bulk_cooccurrences — kwarg renamed ids → entity_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_bulk_cooccurrences_accepts_entity_ids_kwarg() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    e1, e2 = uuid4(), uuid4()
    await api.get_bulk_cooccurrences(entity_ids=[e1, e2])

    assert captured['params']['ids'] == f'{e1},{e2}'


# ---------------------------------------------------------------------------
# get_note_links — batch fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_note_links_batches_per_note() -> None:
    """Two note_ids + two link_types → 4 GETs aggregated into a dict."""
    client = AsyncMock(spec=httpx.AsyncClient)
    gets: list[dict[str, Any]] = []

    async def capture_get(path, params=None, **kwargs):
        gets.append({'path': path, 'params': dict(params or {})})
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    n1, n2 = uuid4(), uuid4()
    out = await api.get_note_links([n1, n2], link_types=['contradicts', 'temporal'])

    assert set(out.keys()) == {n1, n2}
    assert len(gets) == 4
    assert {g['path'] for g in gets} == {f'notes/{n1}/links', f'notes/{n2}/links'}
    assert {g['params'].get('link_type') for g in gets} == {'contradicts', 'temporal'}


@pytest.mark.asyncio
async def test_get_note_links_without_link_types_one_call_per_note() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    gets: list[dict[str, Any]] = []

    async def capture_get(path, params=None, **kwargs):
        gets.append({'path': path, 'params': dict(params or {})})
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    n1 = uuid4()
    await api.get_note_links([n1])

    assert len(gets) == 1
    assert 'link_type' not in gets[0]['params']


# ---------------------------------------------------------------------------
# get_reflection_queue_batch.vault_id / claim_reflection_queue_batch.vault_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_reflection_queue_batch_forwards_vault_id() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    vault_id = uuid4()
    await api.get_reflection_queue_batch(vault_id=vault_id)

    assert captured['params']['vault_id'] == [str(vault_id)]


@pytest.mark.asyncio
async def test_claim_reflection_queue_batch_forwards_vault_id() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_post(path, json=None, params=None, **kwargs):
        captured['params'] = params
        # claim returns NDJSON; reuse the helper.
        return _mock_ndjson_response([])

    client.post = capture_post
    api = RemoteMemexAPI(client)

    vault_id = uuid4()
    await api.claim_reflection_queue_batch(vault_id=vault_id)

    assert captured['params']['vault_id'] == str(vault_id)


@pytest.mark.asyncio
async def test_claim_reflection_queue_batch_omits_vault_id_by_default() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_post(path, json=None, params=None, **kwargs):
        captured['params'] = params
        return _mock_ndjson_response([])

    client.post = capture_post
    api = RemoteMemexAPI(client)

    await api.claim_reflection_queue_batch()

    assert 'vault_id' not in captured['params']


# ---------------------------------------------------------------------------
# kv_list — exclude_prefix / key_prefix / limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kv_list_forwards_prefix_filters() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.kv_list(
        key_prefix='project:abc:',
        exclude_prefix='project:abc:secret',
        limit=25,
    )

    assert captured['params']['key_prefix'] == 'project:abc:'
    assert captured['params']['exclude_prefix'] == 'project:abc:secret'
    assert captured['params']['limit'] == 25


@pytest.mark.asyncio
async def test_kv_list_omits_optional_prefixes_when_none() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.kv_list()

    assert 'exclude_prefix' not in captured['params']
    assert 'key_prefix' not in captured['params']
    # ``limit`` is always sent — it carries the MemexAPI default (100).
    assert captured['params']['limit'] == 100


# ---------------------------------------------------------------------------
# get_memory_links — batch fan-out parity with MemexAPI
# (covers the V4/V5 follow-up; already shipped, re-pinned here)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_memory_links_batches_per_unit_and_link_type() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    gets: list[dict[str, Any]] = []

    async def capture_get(path, params=None, **kwargs):
        gets.append({'path': path, 'params': dict(params or {})})
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    u1, u2 = uuid4(), uuid4()
    out = await api.get_memory_links([u1, u2], link_types=['contradicts'])

    assert set(out.keys()) == {u1, u2}
    assert len(gets) == 2
    assert all(g['params']['link_type'] == 'contradicts' for g in gets)


# ---------------------------------------------------------------------------
# survey — date filters (carry-over check from the survey fix commit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_survey_forwards_date_filters() -> None:
    import datetime as dt

    client, captured = _make_client_capturing(
        post_response=_mock_json_response({'query': 'X', 'sub_queries': [], 'topics': []})
    )
    api = RemoteMemexAPI(client)

    after = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    before = dt.datetime(2026, 12, 31, tzinfo=dt.timezone.utc)
    ref = dt.datetime(2026, 5, 19, tzinfo=dt.timezone.utc)
    await api.survey('query', after=after, before=before, reference_date=ref)

    body = captured['posts'][0]['json']
    assert body['after'] is not None
    assert body['before'] is not None
    assert body['reference_date'] is not None


# ---------------------------------------------------------------------------
# Existing V4 + V5 kwargs — pin once so a regression on them detonates here
# rather than at agent runtime.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_notes_forwards_slim() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_ndjson_response()

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.get_recent_notes(slim=True)

    assert captured['params']['slim'] == 'true'


@pytest.mark.asyncio
async def test_get_entity_mentions_forwards_include_filters() -> None:
    client = AsyncMock(spec=httpx.AsyncClient)
    captured: dict[str, Any] = {}

    async def capture_get(path, params=None, **kwargs):
        captured['params'] = params
        return _mock_json_response([])

    client.get = capture_get
    api = RemoteMemexAPI(client)

    await api.get_entity_mentions(
        uuid4(),
        include_stale=True,
        include_superseded=True,
        include_deprioritized=True,
    )

    assert captured['params']['include_stale'] == 'true'
    assert captured['params']['include_superseded'] == 'true'
    assert captured['params']['include_deprioritized'] == 'true'


# ---------------------------------------------------------------------------
# actor field on deprioritize / restore / consolidate
# ---------------------------------------------------------------------------
#
# The actor kwarg on deprioritize_memory_unit, restore_memory_unit, and
# consolidate_vault is parity with MemexAPI's audit-log attribution.
# Signature parity (test_zero_signature_drift) proves the kwarg exists on
# RemoteMemexAPI; these tests prove it lands in the wire body so the server
# route can forward it to ``request.actor``.


@pytest.mark.asyncio
async def test_deprioritize_memory_unit_forwards_actor() -> None:
    client, captured = _make_client_capturing(
        post_response=_mock_json_response(
            {
                'id': str(uuid4()),
                'note_id': str(uuid4()),
                'text': 'x',
                'fact_type': 'world',
                'status': 'active',
                'mentioned_at': '2026-01-01T00:00:00Z',
                'event_date': None,
                'occurred_start': None,
                'occurred_end': None,
                'vault_id': str(uuid4()),
                'unit_metadata': {},
                'is_deprioritized': True,
            }
        )
    )
    api = RemoteMemexAPI(client)

    vault_id = uuid4()
    unit_id = uuid4()
    await api.deprioritize_memory_unit(unit_id, reason='outdated', vault_id=vault_id, actor='ada')

    body = captured['posts'][0]['json']
    assert body['actor'] == 'ada'


@pytest.mark.asyncio
async def test_restore_memory_unit_forwards_actor() -> None:
    client, captured = _make_client_capturing(
        post_response=_mock_json_response(
            {
                'id': str(uuid4()),
                'note_id': str(uuid4()),
                'text': 'x',
                'fact_type': 'world',
                'status': 'active',
                'mentioned_at': '2026-01-01T00:00:00Z',
                'event_date': None,
                'occurred_start': None,
                'occurred_end': None,
                'vault_id': str(uuid4()),
                'unit_metadata': {},
                'is_deprioritized': False,
            }
        )
    )
    api = RemoteMemexAPI(client)

    vault_id = uuid4()
    unit_id = uuid4()
    await api.restore_memory_unit(unit_id, vault_id=vault_id, actor='ada')

    body = captured['posts'][0]['json']
    assert body['actor'] == 'ada'


@pytest.mark.asyncio
async def test_consolidate_vault_forwards_actor() -> None:
    client, captured = _make_client_capturing(
        post_response=_mock_json_response({'consolidated': 0, 'preview': None})
    )
    api = RemoteMemexAPI(client)

    vault_id = uuid4()
    await api.consolidate_vault(vault_id, dry_run=True, actor='ada')

    body = captured['posts'][0]['json']
    assert body['actor'] == 'ada'
