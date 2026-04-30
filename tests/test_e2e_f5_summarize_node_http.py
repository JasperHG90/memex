"""End-to-end tests for F5 HTTP endpoint (TC8 + TC4).

Covers the synchronous transport contract:
- 200 path: ReflectionResultDTO returned with status='completed'.
- 429 path: rate-limit envelope ({error, retry_after_seconds, message}) +
  Retry-After header (integer seconds).

Engine-path correctness (TC2/TC2.5/TC3) is verified by the engine unit
tests under packages/core/tests/unit/memory/reflect/. The HTTP test
isolates the endpoint contract by overriding ``MemexAPI.summarize_node``
with a fake that returns a canned ReflectionResult or raises
``RateLimitExceededError`` — this keeps the test fast and focused on the
envelope shape rather than the LLM-driven reflection path.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from memex_core.memory.reflect.models import ReflectionResult
from memex_core.memory.sql_models import MentalModel
from memex_core.server.common import get_api
from memex_core.services.rate_limit import RateLimitExceededError
from memex_core.server import app


class _FakeAPI:
    """Minimal MemexAPI stub for endpoint-contract isolation."""

    def __init__(self, *, raise_rate_limit: float | None = None):
        self._raise_rate_limit = raise_rate_limit
        self.calls: list[dict] = []

    async def summarize_node(self, entity_id, *, scope='incremental', vault_id=None):
        self.calls.append({'entity_id': entity_id, 'scope': scope, 'vault_id': vault_id})
        if self._raise_rate_limit is not None:
            raise RateLimitExceededError(
                key=(entity_id, vault_id),
                retry_after_seconds=self._raise_rate_limit,
            )
        return ReflectionResult(
            entity_id=entity_id,
            new_observations=[],
            updated_model=MentalModel(entity_id=entity_id, vault_id=vault_id or uuid4()),
        )


def test_http_200_returns_reflection_result(client: TestClient):
    """TC8a: 200 path returns a ReflectionResultDTO with status='completed'."""
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


def test_http_429_envelope_and_retry_after_header(client: TestClient):
    """TC8b: 429 path carries rate-limit envelope + Retry-After header."""
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
    assert abs(body['retry_after_seconds'] - 42.7) < 1e-6
    assert isinstance(body['message'], str) and body['message']
    # Retry-After is HTTP-standard integer seconds; we ceil-round 42.7 → 43.
    assert resp.headers.get('Retry-After') == '43'


def test_http_full_scope_passes_through(client: TestClient):
    """scope='full' reaches the API method as 'full' (no string mangling)."""
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


def test_http_invalid_scope_rejected_at_boundary(client: TestClient):
    """Pydantic Literal rejects scope outside {'incremental','full'} with 422."""
    resp = client.post(
        '/api/v1/memories/summarize-node',
        json={'entity_id': str(uuid4()), 'scope': 'aggressive'},
    )
    assert resp.status_code == 422
