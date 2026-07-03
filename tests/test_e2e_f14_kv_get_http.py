"""Removal guards for ``GET /api/v1/kv/get`` after the legacy KV-procedure
subsystem was removed entirely (V7 — see "remove the legacy KV-procedure
subsystem entirely").

``procedure:``-prefixed keys are now ordinary KV keys: there is no
version-history envelope and ``include_history`` is a no-op. These
HTTP-level tests pin that removal — GET returns the bare active string,
and ``include_history=true`` is ignored for every key.
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
    return f'global:procedure:write_pr:tag-{uuid4().hex[:8]}'


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


def test_kv_get_include_history_ignored_for_procedure_key(
    client: TestClient, proc_key: str
) -> None:
    """``?include_history=true`` on a procedure: key is now a no-op — the
    legacy history envelope was removed, so the latest value comes back as a
    bare string just like any other key (no {value, version, history})."""
    for v in ('a', 'b', 'c'):
        _put_proc_key(client, proc_key, v)

    response = client.get(
        '/api/v1/kv/get',
        params={'key': proc_key, 'include_history': 'true'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['key'] == proc_key
    assert isinstance(body['value'], str), (
        f'KV-procedure subsystem removed: include_history must NOT return an '
        f'envelope; got {type(body["value"]).__name__}: {body["value"]!r}'
    )
    assert body['value'] == 'c'


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
        params={'key': f'global:procedure:run_tests:does-not-exist-{uuid4().hex[:8]}'},
    )
    assert response.status_code == 404
