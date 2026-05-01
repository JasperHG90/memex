"""End-to-end tests for F29 — POST /api/v1/outcomes/record.

Covers the HTTP wire surface added so non-in-process clients (the Hermes
plugin via ``RemoteMemexAPI``) can call ``MemexAPI.record_outcome``. The MCP
tool calls the API in-process; this route gives remote callers parity.

The F14 ADD-2 invariant (positional ``unit_ids`` + required ``success``)
must hold across the new path — the route body forces ``success`` to be
present and positional fields stay positional in the in-process call.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient


def _seed_unit(postgres_url: str) -> tuple[UUID, UUID]:
    """Insert a MemoryUnit row directly. Returns (unit_id, vault_id)."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _seed() -> tuple[UUID, UUID]:
        conn = await asyncpg.connect(dsn)
        try:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = 'global' LIMIT 1")
            assert vault_row is not None, 'global vault missing — fixture broken'
            vault_id = vault_row['id']
            unit_id = uuid4()
            await conn.execute(
                'INSERT INTO memory_units '
                '(id, vault_id, fact_type, text, status, is_deprioritized, intent_class, '
                'risk_class, confidence, event_date, success_co_count, failure_co_count, '
                'created_at, updated_at) '
                "VALUES ($1, $2, 'observation', $3, 'active', FALSE, 'durable', 'none', 1.0, "
                'NOW(), 0, 0, NOW(), NOW())',
                unit_id,
                vault_id,
                f'F29 test unit {unit_id}',
            )
            return unit_id, vault_id
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_seed())
    finally:
        loop.close()


def _seed_kv_entry(postgres_url: str, key: str) -> None:
    """Insert a kv_entries row so procedure_outcomes' FK to kv_entries.key is satisfied."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _seed() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                'INSERT INTO kv_entries (id, key, value, created_at, updated_at) '
                'VALUES (gen_random_uuid(), $1, $2, NOW(), NOW())',
                key,
                '{"description": "f29 test"}',
            )
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_seed())
    finally:
        loop.close()


def _read_unit_counters(postgres_url: str, unit_id: UUID) -> tuple[int, int]:
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _q() -> tuple[int, int]:
        conn = await asyncpg.connect(dsn)
        try:
            row = await conn.fetchrow(
                'SELECT success_co_count, failure_co_count FROM memory_units WHERE id = $1',
                unit_id,
            )
            assert row is not None
            return row['success_co_count'], row['failure_co_count']
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_q())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# memory_unit happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_memory_unit_success_increments_counter(client: TestClient, postgres_url: str):
    """memory_unit mode: success=true bumps success_co_count."""
    unit_id, vault_id = _seed_unit(postgres_url)
    before_s, before_f = _read_unit_counters(postgres_url, unit_id)
    assert (before_s, before_f) == (0, 0)

    resp = client.post(
        '/api/v1/outcomes/record',
        json={
            'success': True,
            'unit_ids': [str(unit_id)],
            'vault_id': str(vault_id),
            'target_type': 'memory_unit',
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['units_updated'] == 1

    after_s, after_f = _read_unit_counters(postgres_url, unit_id)
    assert (after_s, after_f) == (1, 0)


@pytest.mark.integration
def test_memory_unit_failure_increments_failure_counter(client: TestClient, postgres_url: str):
    """memory_unit mode: success=false bumps failure_co_count, not success."""
    unit_id, vault_id = _seed_unit(postgres_url)

    resp = client.post(
        '/api/v1/outcomes/record',
        json={
            'success': False,
            'unit_ids': [str(unit_id)],
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 200, resp.text
    s, f = _read_unit_counters(postgres_url, unit_id)
    assert (s, f) == (0, 1)


# ---------------------------------------------------------------------------
# kv_key happy path (F14)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_kv_key_mode_writes_procedure_outcomes_row(client: TestClient, postgres_url: str):
    """kv_key mode: increments procedure_outcomes counters and returns the row."""
    _, vault_id = _seed_unit(postgres_url)
    kv_key = f'procedure:test-verb:f29-{uuid4().hex[:8]}'
    _seed_kv_entry(postgres_url, kv_key)

    resp = client.post(
        '/api/v1/outcomes/record',
        json={
            'success': True,
            'kv_key': kv_key,
            'vault_id': str(vault_id),
            'target_type': 'kv_key',
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['kv_key'] == kv_key
    assert body['vault_id'] == str(vault_id)
    assert body['success_co_count'] == 1
    assert body['failure_co_count'] == 0
    assert body.get('last_outcome_at') is not None


# ---------------------------------------------------------------------------
# Bad inputs
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_unknown_vault_returns_400(client: TestClient):
    """vault_id that resolves to nothing returns 400, not 500."""
    resp = client.post(
        '/api/v1/outcomes/record',
        json={
            'success': True,
            'unit_ids': [str(uuid4())],
            'vault_id': f'nonexistent-vault-{uuid4().hex[:8]}',
        },
    )
    assert resp.status_code == 400
    assert 'Unknown vault' in resp.json()['detail']


@pytest.mark.integration
def test_memory_unit_mode_without_unit_ids_returns_400(client: TestClient, postgres_url: str):
    """memory_unit mode without unit_ids → 400 (caller error, not 500)."""
    _, vault_id = _seed_unit(postgres_url)
    resp = client.post(
        '/api/v1/outcomes/record',
        json={
            'success': True,
            'vault_id': str(vault_id),
            'target_type': 'memory_unit',
        },
    )
    assert resp.status_code == 400
    assert 'memory_unit' in resp.json()['detail']


@pytest.mark.integration
def test_kv_key_mode_with_unit_ids_returns_400(client: TestClient, postgres_url: str):
    """Mixing modes (kv_key + unit_ids) is rejected — silent counter divergence guard."""
    unit_id, vault_id = _seed_unit(postgres_url)
    resp = client.post(
        '/api/v1/outcomes/record',
        json={
            'success': True,
            'unit_ids': [str(unit_id)],
            'kv_key': 'procedure:foo:bar',
            'vault_id': str(vault_id),
            'target_type': 'kv_key',
        },
    )
    assert resp.status_code == 400


@pytest.mark.integration
def test_missing_success_returns_422(client: TestClient, postgres_url: str):
    """ADD-2 invariant: success has no default; omitting it is a validation error.

    Mirrors the F14 contract test — a kwargless / minimal call site cannot
    silently record a FAILURE outcome. The HTTP layer must enforce this same
    invariant via Pydantic body validation (Field(...) with no default).
    """
    _, vault_id = _seed_unit(postgres_url)
    resp = client.post(
        '/api/v1/outcomes/record',
        json={
            'unit_ids': [str(uuid4())],
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Auth — write scope required
# ---------------------------------------------------------------------------


READER_KEY = secrets.token_urlsafe(32)
WRITER_KEY = secrets.token_urlsafe(32)


@pytest.fixture()
def auth_client(postgres_container, _truncate_db) -> TestClient:
    """TestClient with auth enabled. Mirrors test_e2e_auth_acl.py fixture."""
    import os
    from urllib.parse import urlparse

    from memex_core.server import app

    dsn = postgres_container.get_connection_url()
    parsed = urlparse(dsn)
    base_env = {
        'MEMEX_LOAD_LOCAL_CONFIG': 'false',
        'MEMEX_LOAD_GLOBAL_CONFIG': 'false',
        'MEMEX_SERVER__META_STORE__TYPE': 'postgres',
        'MEMEX_SERVER__META_STORE__INSTANCE__HOST': parsed.hostname or 'localhost',
        'MEMEX_SERVER__META_STORE__INSTANCE__PORT': str(parsed.port or 5432),
        'MEMEX_SERVER__META_STORE__INSTANCE__DATABASE': parsed.path.lstrip('/'),
        'MEMEX_SERVER__META_STORE__INSTANCE__USER': parsed.username or 'test',
        'MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD': parsed.password or 'test',
        'MEMEX_SERVER__MEMORY__REFLECTION__BACKGROUND_REFLECTION_ENABLED': 'false',
        'MEMEX_SERVER__AUTH__ENABLED': 'true',
        'MEMEX_SERVER__AUTH__KEYS': json.dumps(
            [
                {'key': WRITER_KEY, 'policy': 'writer', 'description': 'test-writer'},
                {'key': READER_KEY, 'policy': 'reader', 'description': 'test-reader'},
            ]
        ),
    }
    with patch.dict(os.environ, base_env):
        with TestClient(app) as c:
            yield c


@pytest.mark.integration
def test_missing_key_returns_401(auth_client: TestClient):
    """Auth enforced — no API key → 401."""
    resp = auth_client.post(
        '/api/v1/outcomes/record',
        json={'success': True, 'unit_ids': [str(uuid4())], 'vault_id': 'global'},
    )
    assert resp.status_code == 401


@pytest.mark.integration
def test_reader_key_returns_403(auth_client: TestClient):
    """Reader policy lacks write scope — 403."""
    resp = auth_client.post(
        '/api/v1/outcomes/record',
        json={'success': True, 'unit_ids': [str(uuid4())], 'vault_id': 'global'},
        headers={'X-API-Key': READER_KEY},
    )
    assert resp.status_code == 403


@pytest.mark.integration
def test_writer_key_accepted(auth_client: TestClient, postgres_url: str):
    """Writer policy is sufficient for /outcomes/record."""
    unit_id, vault_id = _seed_unit(postgres_url)
    resp = auth_client.post(
        '/api/v1/outcomes/record',
        json={
            'success': True,
            'unit_ids': [str(unit_id)],
            'vault_id': str(vault_id),
        },
        headers={'X-API-Key': WRITER_KEY},
    )
    assert resp.status_code == 200, resp.text
