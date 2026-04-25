"""Integration tests for the PR3 batch-dedup behaviour.

These tests cover the load-bearing paths in the RFC-002 design:

- `test_int_create_job_concurrent_overlap` — 10 concurrent `create_job`
  calls with overlapping idempotency keys against the same vault: exactly
  one wins and inserts; the other nine raise `OverlapError`. This proves
  the `pg_advisory_xact_lock` plus the overlap-check `SELECT` together
  close the TOCTOU race; without the lock multiple submitters would all
  pass the overlap check before any commits.

- `test_int_create_job_different_vaults_proceed_in_parallel` — 10 calls
  split 5/5 across two vaults with intra-vault overlap and no cross-vault
  overlap: exactly one succeeds in each vault (2 total). Confirms the
  per-vault scope of the advisory lock; unrelated vaults' submissions
  should not serialize.

- `test_int_batch_overlap_returns_409` — submit batch A, then immediately
  submit batch B with the same idempotency keys via the FastAPI
  TestClient: assert 409, the `Location` header value, and the body
  parses as the Pydantic `BatchJobStatus` schema.

- `test_int_openapi_declares_409_for_batch_endpoint` — fetch
  `/openapi.json` and walk to the batch route's `responses['409']`:
  assert the schema reference and the `Location` header are present.

The tests run against the session-scoped `pgvector/pgvector:pg18-trixie`
testcontainer via the integration `conftest.py` fixtures. Background-task
execution of `_run_job` is patched out — the dedup behaviour we're
testing is at create-time, before any background work runs.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from memex_common.schemas import BatchJobStatus, NoteCreateDTO

from memex_core.processing.batch import OverlapError

# All tests in this module require Postgres.
pytestmark = [pytest.mark.integration]


def _make_note_dto(content: bytes) -> NoteCreateDTO:
    return NoteCreateDTO(
        name='note',
        description='unit test note',
        content=base64.b64encode(content),
    )


@pytest.fixture
def overlap_notes() -> list[NoteCreateDTO]:
    """Five notes whose idempotency keys are stable across calls so concurrent
    `create_job` invocations all derive the same `input_keys` set."""
    return [_make_note_dto(b'overlap-' + str(i).encode()) for i in range(5)]


def _patch_run_job_to_noop(api: Any) -> Any:
    """Patch `JobManager._run_job` to a no-op AsyncMock so the dedup test
    behaviour is observed at create-time. Without this the background task
    would advance the row through PROCESSING → COMPLETED, and a second
    submission would not detect overlap (COMPLETED jobs don't block).
    Returns the patcher so the caller can stop() it.
    """
    return patch.object(api.batch_manager, '_run_job', new=AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_int_create_job_concurrent_overlap(api, metastore, init_global_vault, overlap_notes):
    """AC-020 (rev) load-bearing: 10 concurrent `create_job` calls with the
    same idempotency keys in the same vault. Exactly one returns a job_id;
    the other nine raise `OverlapError`. Without the per-vault advisory
    lock the dedup check is racy and 2+ jobs can be inserted.
    """
    await api.initialize()

    with _patch_run_job_to_noop(api):
        results = await asyncio.gather(
            *[api.batch_manager.create_job(notes=overlap_notes) for _ in range(10)],
            return_exceptions=True,
        )

    successes = [r for r in results if not isinstance(r, BaseException)]
    overlap_errors = [r for r in results if isinstance(r, OverlapError)]
    other_errors = [
        r for r in results if isinstance(r, BaseException) and not isinstance(r, OverlapError)
    ]

    assert other_errors == [], (
        f'Unexpected non-OverlapError exceptions: {other_errors!r}. '
        'A different exception means the advisory lock or the overlap query '
        'has a real bug; investigate before adjusting test expectations.'
    )
    assert len(successes) == 1, (
        f'Exactly 1 of 10 calls should succeed, got {len(successes)}. '
        'If this fails with N>1, the advisory lock is not closing the race.'
    )
    assert len(overlap_errors) == 9, (
        f'The other 9 calls must raise OverlapError, got {len(overlap_errors)}. '
        f'Errors: {overlap_errors!r}'
    )

    winning_job_id = successes[0]
    for err in overlap_errors:
        assert err.existing_id == winning_job_id, (
            'all OverlapErrors must reference the single winner; got '
            f'existing_id={err.existing_id} vs winner={winning_job_id}'
        )
        # The status the loser saw should be a real in-flight status.
        assert err.status in ('pending', 'processing'), err.status


@pytest.mark.asyncio
async def test_int_create_job_different_vaults_proceed_in_parallel(
    api, metastore, init_global_vault
):
    """AC-020 (rev): two unrelated vaults must NOT serialize against each
    other on `pg_advisory_xact_lock(hashtext(:vault_id::text))`. Run 5
    overlapping submissions per vault concurrently across two vaults; expect
    exactly 1 success per vault (2 total successes) — i.e., dedup works
    intra-vault but cross-vault submissions proceed in parallel.
    """
    from memex_core.memory.sql_models import Vault

    await api.initialize()

    vault_a = uuid4()
    vault_b = uuid4()
    async with metastore.session() as session:
        session.add(Vault(id=vault_a, name='vault-a'))
        session.add(Vault(id=vault_b, name='vault-b'))
        await session.commit()

    notes_a = [_make_note_dto(b'a-' + str(i).encode()) for i in range(3)]
    notes_b = [_make_note_dto(b'b-' + str(i).encode()) for i in range(3)]

    with _patch_run_job_to_noop(api):
        results = await asyncio.gather(
            *[api.batch_manager.create_job(notes=notes_a, vault_id=vault_a) for _ in range(5)],
            *[api.batch_manager.create_job(notes=notes_b, vault_id=vault_b) for _ in range(5)],
            return_exceptions=True,
        )

    successes = [r for r in results if not isinstance(r, BaseException)]
    overlap_errors = [r for r in results if isinstance(r, OverlapError)]
    other_errors = [
        r for r in results if isinstance(r, BaseException) and not isinstance(r, OverlapError)
    ]

    assert other_errors == [], f'Unexpected exceptions: {other_errors!r}'
    assert len(successes) == 2, (
        f'Expected exactly 1 success per vault (2 total), got {len(successes)}. '
        'If <2: advisory lock is over-serialising across vaults '
        '(check the hashtext key); if >2: dedup is not working intra-vault.'
    )
    assert len(overlap_errors) == 8, f'Expected 8 OverlapErrors, got {len(overlap_errors)}'


@pytest.mark.asyncio
async def test_int_batch_overlap_returns_409(api, metastore, init_global_vault, overlap_notes):
    """AC-021: the HTTP layer translates `OverlapError` to 409 with a
    `BatchJobStatus`-shaped body and `Location: /api/v1/ingestions/<id>`.
    """
    from memex_core.server import app

    await api.initialize()
    app.state.api = api

    payload = {
        'notes': [
            {
                'name': 'note',
                'description': 'unit test note',
                # Re-encode as base64 string for HTTP transport (Base64Bytes accepts str).
                'content': base64.b64encode(b'overlap-' + str(i).encode()).decode('ascii'),
            }
            for i in range(len(overlap_notes))
        ]
    }

    with _patch_run_job_to_noop(api):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            first = await ac.post('/api/v1/ingestions/batch', json=payload)
            assert first.status_code == 202, first.text
            first_body = first.json()
            first_job_id = first_body['job_id']
            assert first_body['status'] == 'pending'

            # Second submission with the same payload must trip the overlap check.
            second = await ac.post('/api/v1/ingestions/batch', json=payload)

    assert second.status_code == 409, second.text

    # AC-021 (b): Location header points at the existing job's status endpoint.
    assert second.headers['location'] == f'/api/v1/ingestions/{first_job_id}', (
        f'Expected Location header /api/v1/ingestions/{first_job_id}, '
        f'got {second.headers.get("location")!r}'
    )

    # AC-021 (c): body parses as the Pydantic BatchJobStatus.
    parsed = BatchJobStatus.model_validate(second.json())
    assert str(parsed.job_id) == first_job_id, (
        f'409 body job_id must match the existing job; got {parsed.job_id} vs {first_job_id}'
    )
    assert parsed.status in ('pending', 'processing')


@pytest.mark.asyncio
async def test_int_openapi_declares_409_for_batch_endpoint(api, init_global_vault):
    """AC-021 (d): the route's OpenAPI declaration must include a 409
    response referencing the `BatchJobStatus` schema and the `Location`
    header. Catches a misconfigured `responses=` kwarg on the route.
    """
    from memex_core.server import app

    await api.initialize()
    app.state.api = api

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get('/openapi.json')
    assert resp.status_code == 200, resp.text
    spec = resp.json()

    batch_post = spec['paths']['/api/v1/ingestions/batch']['post']
    responses = batch_post['responses']
    assert '409' in responses, (
        f'409 response missing from /api/v1/ingestions/batch OpenAPI spec; '
        f'got responses={list(responses)}.'
    )
    r409 = responses['409']

    # The body schema must reference BatchJobStatus. FastAPI emits a $ref
    # like `#/components/schemas/BatchJobStatus`.
    json_schema = r409['content']['application/json']['schema']
    ref = json_schema.get('$ref') or json_schema.get('allOf', [{}])[0].get('$ref', '')
    assert 'BatchJobStatus' in ref, (
        f'409 body schema must reference BatchJobStatus; got schema={json_schema!r}'
    )

    # Location header must be declared so OpenAPI consumers can discover it.
    assert 'Location' in r409.get('headers', {}), (
        f'409 must declare a Location header; got headers={r409.get("headers")!r}'
    )
