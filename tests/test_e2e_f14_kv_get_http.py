"""End-to-end tests for F14 ``GET /api/v1/kv/get?include_history=...``.

Two contracts to lock:

1. **Default (back-compat)**: GET without ``include_history`` returns the
   existing :class:`KVEntryDTO` shape — for procedure: keys, the ``value``
   field is the unwrapped active string, identical to non-procedure keys.
2. **include_history=true**: GET returns
   :class:`KVProcedureEntryDTO` — same outer envelope, but ``value`` is now
   ``{value, version, history}``. Non-procedure keys ignore the flag.

These are HTTP-level tests so the wire shape is what we lock down — the
service layer is exercised separately by the unit + integration tests.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def _put_proc_key(client: TestClient, key: str, value: str) -> None:
    response = client.put('/api/v1/kv', json={'key': key, 'value': value})
    assert response.status_code == 200, response.text


@pytest.fixture
def proc_key() -> str:
    return f'procedure:write_pr:tag-{uuid4().hex[:8]}'


def test_kv_get_default_shape_unchanged_for_procedure_key(
    client: TestClient, proc_key: str
) -> None:
    """Default GET on a procedure: key returns KVEntryDTO with the active value as a string."""
    _put_proc_key(client, proc_key, 'first-value')
    _put_proc_key(client, proc_key, 'second-value')

    response = client.get('/api/v1/kv/get', params={'key': proc_key})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['key'] == proc_key
    assert isinstance(body['value'], str), (
        f'expected default kv_get to return value: str (back-compat); got '
        f'{type(body["value"]).__name__}: {body["value"]!r}'
    )
    assert body['value'] == 'second-value'


def test_kv_get_include_history_true_returns_envelope(client: TestClient, proc_key: str) -> None:
    """``?include_history=true`` returns KVProcedureEntryDTO with structured value."""
    for v in ('a', 'b', 'c'):
        _put_proc_key(client, proc_key, v)

    response = client.get(
        '/api/v1/kv/get',
        params={'key': proc_key, 'include_history': 'true'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['key'] == proc_key
    value = body['value']
    assert isinstance(value, dict), (
        f'expected include_history=true to return value: dict; got {type(value).__name__}'
    )
    assert value['value'] == 'c'
    assert value['version'] == 3
    history = value['history']
    assert [h['v'] for h in history] == [1, 2]
    assert [h['value'] for h in history] == ['a', 'b']


def test_kv_get_include_history_ignored_for_non_procedure_key(
    client: TestClient,
) -> None:
    """``include_history=true`` on a non-procedure key still returns plain KVEntryDTO."""
    plain_key = f'global:test:plain:{uuid4().hex[:8]}'
    response = client.put('/api/v1/kv', json={'key': plain_key, 'value': 'plain-value'})
    assert response.status_code == 200

    response = client.get(
        '/api/v1/kv/get',
        params={'key': plain_key, 'include_history': 'true'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body['value'], str)
    assert body['value'] == 'plain-value'


def test_kv_get_returns_404_when_missing(client: TestClient) -> None:
    """Unknown key still 404s — F14 didn't change this."""
    response = client.get(
        '/api/v1/kv/get',
        params={'key': f'procedure:run_tests:does-not-exist-{uuid4().hex[:8]}'},
    )
    assert response.status_code == 404


def test_kv_procedure_observations_route_returns_list(
    client: TestClient,
) -> None:
    """``GET /api/v1/kv/procedure-observations`` returns a JSON list.

    Endpoint contract is exercised here (route + auth + response_model). The
    rich ranking semantics (mw_score ordering, limit cap, cross-vault
    isolation, context filter) are covered by the integration tests in
    ``test_int_f14_top_outcomes.py`` — those exercise the SQL path, this
    one locks the wire shape.
    """
    from memex_common.config import GLOBAL_VAULT_ID

    response = client.get(
        '/api/v1/kv/procedure-observations',
        params={'vault_id': str(GLOBAL_VAULT_ID), 'limit': 5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)


def test_kv_procedure_observations_rejects_invalid_limit(
    client: TestClient,
) -> None:
    """``limit`` must be 1-20."""
    from memex_common.config import GLOBAL_VAULT_ID

    response = client.get(
        '/api/v1/kv/procedure-observations',
        params={'vault_id': str(GLOBAL_VAULT_ID), 'limit': 0},
    )
    assert response.status_code == 422

    response = client.get(
        '/api/v1/kv/procedure-observations',
        params={'vault_id': str(GLOBAL_VAULT_ID), 'limit': 21},
    )
    assert response.status_code == 422


def test_kv_procedure_observations_rejects_invalid_vault_id(
    client: TestClient,
) -> None:
    """Non-UUID ``vault_id`` returns a 4xx (ValueError → handled error)."""
    response = client.get(
        '/api/v1/kv/procedure-observations',
        params={'vault_id': 'not-a-uuid', 'limit': 5},
    )
    assert response.status_code in (400, 422)
