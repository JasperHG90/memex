"""End-to-end tests for the atomic note-append endpoint.

These tests drive the full stack: FastAPI route → MemexAPI facade →
IngestionService.append_to_note → real PostgreSQL → incremental block-diff
extraction (LLM mock) → audit row. They guard against wiring regressions
that the route-level mocks in test_int_append_route.py and the service-level
tests in test_int_note_append.py cannot catch.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.server import app

VAULT = str(GLOBAL_VAULT_ID)


def _create_parent(client: TestClient, note_key: str, body: str) -> UUID:
    payload = {
        'name': 'Parent Note',
        'description': 'parent',
        'content': base64.b64encode(body.encode()).decode(),
        'note_key': note_key,
    }
    r = client.post('/api/v1/ingestions', json=payload)
    assert r.status_code == 200, f'Failed to seed parent: {r.text}'
    return UUID(r.json()['note_id'])


def _append(client: TestClient, *, note_key: str | None = None, **kwargs: Any) -> httpx.Response:
    payload: dict[str, Any] = {'append_id': str(uuid4())}
    if note_key is not None:
        payload['note_key'] = note_key
        payload['vault_id'] = VAULT
    payload.update(kwargs)
    return client.post('/api/v1/notes/append', json=payload)


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_append_via_http_grows_body(client: TestClient) -> None:
    note_key = f'e2e-append-{uuid4()}'
    parent_id = _create_parent(client, note_key, '# Day 1\n\nfirst line.\n')

    r = _append(client, note_key=note_key, delta='second line.', joiner='paragraph')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['status'] == 'success'
    assert UUID(body['note_id']) == parent_id

    show = client.get(f'/api/v1/notes/{parent_id}')
    assert show.status_code == 200
    note = show.json()
    assert 'first line.' in note['original_text']
    assert 'second line.' in note['original_text']
    assert note['original_text'].index('first line.') < note['original_text'].index('second line.')


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_append_replay_returns_replayed_status(client: TestClient) -> None:
    note_key = f'e2e-replay-{uuid4()}'
    _create_parent(client, note_key, 'base body')
    append_id = str(uuid4())

    payload = {
        'note_key': note_key,
        'vault_id': VAULT,
        'delta': 'only-once',
        'append_id': append_id,
    }
    r1 = client.post('/api/v1/notes/append', json=payload)
    assert r1.status_code == 200
    assert r1.json()['status'] == 'success'

    r2 = client.post('/api/v1/notes/append', json=payload)
    assert r2.status_code == 200
    assert r2.json()['status'] == 'replayed'
    assert UUID(r2.json()['note_id']) == UUID(r1.json()['note_id'])
    assert UUID(r2.json()['append_id']) == UUID(append_id)


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_append_id_conflict_returns_409(client: TestClient) -> None:
    note_key = f'e2e-conflict-{uuid4()}'
    _create_parent(client, note_key, 'body')
    append_id = str(uuid4())

    r1 = client.post(
        '/api/v1/notes/append',
        json={'note_key': note_key, 'vault_id': VAULT, 'delta': 'first', 'append_id': append_id},
    )
    assert r1.status_code == 200

    r2 = client.post(
        '/api/v1/notes/append',
        json={
            'note_key': note_key,
            'vault_id': VAULT,
            'delta': 'DIFFERENT',
            'append_id': append_id,
        },
    )
    assert r2.status_code == 409


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_append_to_archived_parent_returns_409(client: TestClient) -> None:
    note_key = f'e2e-archived-{uuid4()}'
    parent_id = _create_parent(client, note_key, 'body')

    r = client.patch(f'/api/v1/notes/{parent_id}/status', json={'status': 'archived'})
    assert r.status_code == 200, r.text

    r = _append(client, note_key=note_key, delta='late delta')
    assert r.status_code == 409


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_append_validation_errors(client: TestClient) -> None:
    note_key = f'e2e-validation-{uuid4()}'
    _create_parent(client, note_key, 'body')

    assert _append(client, note_key=note_key, delta='   ').status_code == 422
    assert _append(client, note_key=note_key, delta='---\n+ frontmatter').status_code == 422
    assert _append(client, note_key=note_key, delta='has \x00 nul').status_code == 422

    r = client.post(
        '/api/v1/notes/append',
        json={
            'note_key': note_key,
            'vault_id': VAULT,
            'note_id': str(uuid4()),
            'delta': 'ambiguous',
            'append_id': str(uuid4()),
        },
    )
    assert r.status_code == 422


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_append_unknown_parent_returns_404(client: TestClient) -> None:
    r = client.post(
        '/api/v1/notes/append',
        json={
            'note_id': str(uuid4()),
            'delta': 'orphan delta',
            'append_id': str(uuid4()),
        },
    )
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_append_idempotent_replay_after_committed_call(client: TestClient) -> None:
    note_key = f'e2e-audit-{uuid4()}'
    parent_id = _create_parent(client, note_key, 'base')
    append_id = str(uuid4())
    payload = {'note_key': note_key, 'vault_id': VAULT, 'delta': 'audited', 'append_id': append_id}

    assert client.post('/api/v1/notes/append', json=payload).status_code == 200
    r2 = client.post('/api/v1/notes/append', json=payload)
    assert r2.status_code == 200
    assert r2.json()['status'] == 'replayed'
    assert UUID(r2.json()['note_id']) == parent_id


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_append_50_serial_grows_body_correctly(client: TestClient) -> None:
    note_key = f'e2e-stress-{uuid4()}'
    parent_id = _create_parent(client, note_key, 'header')

    sentinels = [f'pulse-{i}' for i in range(50)]
    for s in sentinels:
        r = _append(client, note_key=note_key, delta=s)
        assert r.status_code == 200, f'Append #{s} failed: {r.status_code} {r.text}'

    show = client.get(f'/api/v1/notes/{parent_id}')
    assert show.status_code == 200
    body_text = show.json()['original_text']
    for s in sentinels:
        assert s in body_text, f'Missing sentinel {s} in body'
    assert body_text.startswith('header')


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_concurrent_distinct_appends_serialise(client: TestClient) -> None:
    note_key = f'e2e-concurrent-{uuid4()}'
    parent_id = _create_parent(client, note_key, 'header')

    deltas = [f'concurrent-{i}' for i in range(5)]

    def _post(delta: str) -> httpx.Response:
        with TestClient(app) as tc:
            return tc.post(
                '/api/v1/notes/append',
                json={
                    'note_key': note_key,
                    'vault_id': VAULT,
                    'delta': delta,
                    'append_id': str(uuid4()),
                    'joiner': 'newline',
                },
            )

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(_post, deltas))

    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text) for r in responses if r.status_code != 200
    ]

    show = client.get(f'/api/v1/notes/{parent_id}')
    body_text = show.json()['original_text']
    for d in deltas:
        assert d in body_text


@pytest.mark.integration
@pytest.mark.llm
def test_e2e_concurrent_same_append_id_is_idempotent(client: TestClient) -> None:
    note_key = f'e2e-same-id-{uuid4()}'
    parent_id = _create_parent(client, note_key, 'base')
    append_id = str(uuid4())
    payload = {
        'note_key': note_key,
        'vault_id': VAULT,
        'delta': 'only-once',
        'append_id': append_id,
    }

    def _post(_: int) -> httpx.Response:
        with TestClient(app) as tc:
            return tc.post('/api/v1/notes/append', json=payload)

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(_post, range(5)))

    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
    statuses = [r.json()['status'] for r in responses]
    assert statuses.count('success') >= 1
    assert statuses.count('replayed') >= 1
    assert all(s in ('success', 'replayed') for s in statuses)

    show = client.get(f'/api/v1/notes/{parent_id}')
    body_text = show.json()['original_text']
    assert body_text.count('only-once') == 1
