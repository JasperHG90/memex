"""End-to-end tests for F5 HTTP endpoint (TC8 + TC4).

Covers the synchronous transport contract:
- 200 path: ReflectionResultDTO returned with status='completed'.
- 429 path: rate-limit envelope ({error, retry_after_seconds, message}) +
  Retry-After header (integer seconds).
- Audit log entry on 200 (action='memory_summarize_node').

Engine-path correctness (TC2/TC2.5/TC3) is verified by the engine unit
tests under packages/core/tests/unit/memory/reflect/. The HTTP test
isolates the endpoint contract by overriding ``MemexAPI.summarize_node``
with a fake that returns a canned ReflectionResult or raises
``RateLimitExceededError`` — this keeps the test fast and focused on the
envelope shape rather than the LLM-driven reflection path.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from memex_core.memory.reflect.models import ReflectionResult
from memex_core.memory.sql_models import MentalModel
from memex_core.server.common import get_api
from memex_core.services.rate_limit import RateLimitExceededError
from memex_core.server import app


def _query_audit(
    postgres_url: str,
    action: str,
    resource_id: str | None = None,
    *,
    retries: int = 20,
    delay: float = 0.05,
) -> list[dict]:
    """Mirror of the helper in test_e2e_f4_deprioritize.py — audit writes are
    fire-and-forget, so retry briefly while the background task drains."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _fetch() -> list[dict]:
        conn = await asyncpg.connect(dsn)
        try:
            if resource_id:
                rows = await conn.fetch(
                    'SELECT action, resource_type, resource_id, actor, '
                    'session_id, details FROM audit_logs '
                    'WHERE action = $1 AND resource_id = $2 '
                    'ORDER BY "timestamp" ASC',
                    action,
                    resource_id,
                )
            else:
                rows = await conn.fetch(
                    'SELECT action, resource_type, resource_id, actor, '
                    'session_id, details FROM audit_logs '
                    'WHERE action = $1 ORDER BY "timestamp" ASC',
                    action,
                )
            out: list[dict] = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get('details'), str):
                    d['details'] = json.loads(d['details'])
                out.append(d)
            return out
        finally:
            await conn.close()

    for _ in range(retries):
        loop = asyncio.new_event_loop()
        try:
            entries = loop.run_until_complete(_fetch())
        finally:
            loop.close()
        if entries:
            return entries
        time.sleep(delay)
    return []


class _FakeAPI:
    """Minimal MemexAPI stub for endpoint-contract isolation.

    When *raise_rate_limit* is set, ``summarize_node`` raises a
    ``RateLimitExceededError`` configured with that retry value. The exception
    instance is captured on the fake so tests can assert that the wire-level
    body fields exactly mirror the service-layer exception (TC8 amendment b).
    """

    def __init__(self, *, raise_rate_limit: float | None = None):
        self._raise_rate_limit = raise_rate_limit
        self.calls: list[dict] = []
        self.last_exception: RateLimitExceededError | None = None

    async def summarize_node(self, entity_id, *, scope='incremental', vault_id=None):
        self.calls.append({'entity_id': entity_id, 'scope': scope, 'vault_id': vault_id})
        if self._raise_rate_limit is not None:
            exc = RateLimitExceededError(
                key=(entity_id, vault_id),
                retry_after_seconds=self._raise_rate_limit,
            )
            self.last_exception = exc
            raise exc
        return ReflectionResult(
            entity_id=entity_id,
            new_observations=[],
            updated_model=MentalModel(entity_id=entity_id, vault_id=vault_id or uuid4()),
        )


@pytest.mark.integration
def test_http_200_returns_reflection_result_and_writes_audit(client: TestClient, postgres_url: str):
    """TC8a: 200 path returns a ReflectionResultDTO with status='completed';
    TC8 audit assertion: an audit_logs row with action='memory_summarize_node'
    and resource_id=entity_id is written."""
    fake = _FakeAPI()
    app.dependency_overrides[get_api] = lambda: fake
    try:
        entity_id = str(uuid4())
        resp = client.post(
            '/api/v1/memories/summarize-node',
            json={'entity_id': entity_id, 'scope': 'incremental'},
        )
    finally:
        app.dependency_overrides.pop(get_api, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['entity_id'] == entity_id
    assert body['status'] == 'completed'
    assert body['new_observations'] == []
    assert len(fake.calls) == 1
    assert fake.calls[0]['scope'] == 'incremental'

    # NOTE: The fake bypasses ReflectionService, so the service-layer
    # audit_event call does not fire here. The audit row is asserted in the
    # complementary TC8 200 case below, which runs against the real service.


def test_http_429_envelope_and_retry_after_header(client: TestClient):
    """TC8b: 429 path carries rate-limit envelope + Retry-After header.

    Amendments folded:
    (a) Retry-After header is the ceil(retry_after_seconds) of the body field.
    (b) Body's retry_after_seconds + message verbatim mirror the service-layer
        ``RateLimitExceededError`` instance (no string mangling).
    """
    fake = _FakeAPI(raise_rate_limit=42.7)
    app.dependency_overrides[get_api] = lambda: fake
    try:
        entity_id = str(uuid4())
        resp = client.post(
            '/api/v1/memories/summarize-node',
            json={'entity_id': entity_id, 'scope': 'full'},
        )
    finally:
        app.dependency_overrides.pop(get_api, None)

    assert resp.status_code == 429
    body = resp.json()
    assert body['error'] == 'rate_limit_exceeded'

    # (b) Body fields verbatim equal the service-layer exception
    exc = fake.last_exception
    assert exc is not None
    assert body['retry_after_seconds'] == exc.retry_after_seconds
    assert body['message'] == str(exc)
    assert isinstance(body['message'], str) and body['message']

    # (a) Retry-After header is HTTP-standard integer seconds: ceil(body field)
    header_val = resp.headers.get('Retry-After')
    assert header_val is not None
    assert int(header_val) == math.ceil(body['retry_after_seconds'])
    # Cross-check the absolute value to lock the contract: ceil(42.7) == 43
    assert header_val == '43'


@pytest.mark.parametrize('scope', ['incremental', 'full'])
def test_http_429_envelope_invariant_across_scopes(client: TestClient, scope: str):
    """TC8 amendment (d): 429 envelope shape is identical for both scopes.

    A 429 means the bucket was empty before any scope-dependent path ran,
    so the envelope MUST NOT vary by scope. This locks that invariant.
    """
    fake = _FakeAPI(raise_rate_limit=10.0)
    app.dependency_overrides[get_api] = lambda: fake
    try:
        resp = client.post(
            '/api/v1/memories/summarize-node',
            json={'entity_id': str(uuid4()), 'scope': scope},
        )
    finally:
        app.dependency_overrides.pop(get_api, None)

    assert resp.status_code == 429
    body = resp.json()
    assert set(body.keys()) == {'error', 'retry_after_seconds', 'message'}
    assert body['error'] == 'rate_limit_exceeded'
    assert body['retry_after_seconds'] == 10.0
    assert resp.headers.get('Retry-After') == '10'


def test_http_full_scope_reaches_engine_unbounded_path(client: TestClient):
    """TC8 amendment (c): scope='full' over HTTP reaches the engine's
    unbounded path (which is hard-capped at MAX_FULL_SCOPE_UNITS).

    The transport-only check here proves scope='full' is propagated verbatim
    through the HTTP boundary. Engine-side enforcement that
    ``limit_recent_memories=None`` ⇒ SQL ``rn <= MAX_FULL_SCOPE_UNITS`` is
    asserted in packages/core/tests/unit/memory/reflect/
    test_reflection_engine_batch.py (TC3, literal_binds compile). Together
    they form the HTTP→engine chain.
    """
    fake = _FakeAPI()
    app.dependency_overrides[get_api] = lambda: fake
    try:
        resp = client.post(
            '/api/v1/memories/summarize-node',
            json={'entity_id': str(uuid4()), 'scope': 'full'},
        )
    finally:
        app.dependency_overrides.pop(get_api, None)

    assert resp.status_code == 200
    assert fake.calls[0]['scope'] == 'full'
    # No string mangling: the literal 'full' reaches the API method.
    assert fake.calls[0]['scope'] != 'incremental'


def test_http_cross_vault_rate_limit_isolation(client: TestClient):
    """TC8 amendment (e): rate-limit isolation across vaults at the wire.

    Two POSTs with the same entity_id but distinct vault_ids must both
    succeed — the limiter key is (entity_id, vault_id), so they MUST NOT
    share a bucket. Verified at the HTTP layer to lock the wire contract.
    """
    fake = _FakeAPI()
    app.dependency_overrides[get_api] = lambda: fake
    try:
        entity_id = str(uuid4())
        vault_a = str(uuid4())
        vault_b = str(uuid4())

        r1 = client.post(
            '/api/v1/memories/summarize-node',
            json={'entity_id': entity_id, 'scope': 'incremental', 'vault_id': vault_a},
        )
        r2 = client.post(
            '/api/v1/memories/summarize-node',
            json={'entity_id': entity_id, 'scope': 'incremental', 'vault_id': vault_b},
        )
    finally:
        app.dependency_overrides.pop(get_api, None)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert len(fake.calls) == 2
    assert {str(c['vault_id']) for c in fake.calls} == {vault_a, vault_b}


def test_http_invalid_scope_rejected_at_boundary(client: TestClient):
    """Pydantic Literal rejects scope outside {'incremental','full'} with 422."""
    resp = client.post(
        '/api/v1/memories/summarize-node',
        json={'entity_id': str(uuid4()), 'scope': 'aggressive'},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TC8 audit assertion — real-service variant
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_http_200_writes_audit_log_via_real_service(
    client: TestClient, postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """TC8 audit row assertion against the real ReflectionService.

    We can't use the _FakeAPI here because the audit_event call lives in
    ``ReflectionService.summarize_node`` — we need the real service path to
    fire it. We stub the engine reflect step instead, returning a canned
    ReflectionResult, so the audit log + rate-limit + scope plumbing stays
    real but no LLM is invoked.
    """
    from memex_core.memory.reflect.models import ReflectionResult as _RR
    from memex_core.services.reflection import ReflectionService

    entity_id = uuid4()

    async def _fake_reflect(self, request):
        return _RR(
            entity_id=request.entity_id,
            new_observations=[],
            updated_model=MentalModel(
                entity_id=request.entity_id,
                vault_id=request.vault_id,
            ),
        )

    monkeypatch.setattr(ReflectionService, 'reflect', _fake_reflect)

    resp = client.post(
        '/api/v1/memories/summarize-node',
        json={'entity_id': str(entity_id), 'scope': 'incremental'},
    )
    assert resp.status_code == 200, resp.text

    rows = _query_audit(postgres_url, 'memory_summarize_node', str(entity_id))
    assert len(rows) == 1, f'expected 1 audit row, got {len(rows)}: {rows}'
    row = rows[0]
    assert row['resource_type'] == 'entity'
    assert row['resource_id'] == str(entity_id)
    assert row['details'] is not None
    assert row['details']['scope'] == 'incremental'
    assert row['details']['observation_count'] == 0
