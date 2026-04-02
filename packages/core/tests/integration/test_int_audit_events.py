"""Integration tests for service-layer domain audit events (AC-022, AC-023).

AC-022: A single mutation produces exactly two audit log entries sharing session_id.
AC-023: Domain events are emitted only after successful mutations, not on failure.
"""

import asyncio
import json
import os
import time
from typing import Generator
from unittest.mock import patch
from urllib.parse import urlparse

import asyncpg
import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

VALID_KEY = 'integration-test-key-1234567890'
KEY_PREFIX = VALID_KEY[:8] + '...'
KEY_DESCRIPTION = 'test-agent'


def _build_env_vars(container: PostgresContainer) -> dict[str, str]:
    dsn = container.get_connection_url()
    parsed = urlparse(dsn)
    return {
        'MEMEX_LOAD_LOCAL_CONFIG': 'false',
        'MEMEX_LOAD_GLOBAL_CONFIG': 'false',
        'MEMEX_SERVER__META_STORE__TYPE': 'postgres',
        'MEMEX_SERVER__META_STORE__INSTANCE__HOST': parsed.hostname or 'localhost',
        'MEMEX_SERVER__META_STORE__INSTANCE__PORT': str(parsed.port or 5432),
        'MEMEX_SERVER__META_STORE__INSTANCE__DATABASE': parsed.path.lstrip('/'),
        'MEMEX_SERVER__META_STORE__INSTANCE__USER': parsed.username or 'test',
        'MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD': parsed.password or 'test',
        'MEMEX_SERVER__MEMORY__REFLECTION__BACKGROUND_REFLECTION_ENABLED': 'false',
    }


@pytest.fixture()
def audit_client(
    postgres_container: PostgresContainer,
) -> Generator[TestClient, None, None]:
    """TestClient with auth enabled and real middleware stack."""
    env = {
        **_build_env_vars(postgres_container),
        'MEMEX_SERVER__AUTH__ENABLED': 'true',
        'MEMEX_SERVER__AUTH__KEYS': json.dumps(
            [{'key': VALID_KEY, 'policy': 'admin', 'description': KEY_DESCRIPTION}]
        ),
    }
    from memex_core.server import app

    with patch.dict(os.environ, env):
        with TestClient(app) as c:
            yield c


def _query_audit_logs(
    postgres_url: str,
    action: str | None = None,
    session_id: str | None = None,
) -> list[dict]:
    """Query audit_logs table via asyncpg."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _fetch():
        conn = await asyncpg.connect(dsn)
        try:
            conditions = []
            params = []
            idx = 1
            if action is not None:
                conditions.append(f'action = ${idx}')
                params.append(action)
                idx += 1
            if session_id is not None:
                conditions.append(f'session_id = ${idx}')
                params.append(session_id)
                idx += 1

            where = f'WHERE {" AND ".join(conditions)}' if conditions else ''
            rows = await conn.fetch(
                f'SELECT action, actor, session_id, resource_type, resource_id, details '
                f'FROM audit_logs {where} '
                f'ORDER BY "timestamp" DESC',
                *params,
            )
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get('details'), str):
                    d['details'] = json.loads(d['details'])
                result.append(d)
            return result
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_fetch())
    finally:
        loop.close()


def _clear_audit_logs(postgres_url: str) -> None:
    """Truncate audit_logs table."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _truncate():
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute('TRUNCATE TABLE audit_logs')
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_truncate())
    finally:
        loop.close()


def _wait_for_audit_logs(postgres_url: str, expected_count: int, timeout: float = 2.0) -> list:
    """Poll audit_logs until expected_count entries appear or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entries = _query_audit_logs(postgres_url)
        if len(entries) >= expected_count:
            return entries
        time.sleep(0.1)
    return _query_audit_logs(postgres_url)


@pytest.fixture(autouse=True)
def clean_audit_logs(postgres_uri: str) -> Generator[None, None, None]:
    """Truncate audit_logs before each test."""
    _clear_audit_logs(postgres_uri)
    yield


@pytest.mark.integration
class TestAuditCorrelation:
    """AC-022: HTTP access log + domain event share session_id."""

    def test_vault_create_produces_two_entries(
        self, audit_client: TestClient, postgres_uri: str
    ) -> None:
        """Creating a vault via HTTP produces http.request + vault.created with same session_id."""
        session_id = 'correlation-test-vault-create'
        resp = audit_client.post(
            '/api/v1/vaults',
            json={'name': 'audit-test-vault', 'description': 'test'},
            headers={
                'X-API-Key': VALID_KEY,
                'X-Session-ID': session_id,
            },
        )
        assert resp.status_code == 200

        entries = _wait_for_audit_logs(postgres_uri, expected_count=2)
        session_entries = [e for e in entries if e['session_id'] == session_id]
        assert len(session_entries) == 2

        actions = {e['action'] for e in session_entries}
        assert 'http.request' in actions
        assert 'vault.created' in actions


@pytest.mark.integration
class TestSuccessOnlyEmission:
    """AC-023: Domain events are emitted only after successful mutations."""

    def test_no_domain_event_on_failed_delete(
        self, audit_client: TestClient, postgres_uri: str
    ) -> None:
        """Deleting a nonexistent note produces only http.request, no note.deleted."""
        session_id = 'success-only-test-delete'
        fake_note_id = '00000000-0000-0000-0000-000000000099'
        resp = audit_client.delete(
            f'/api/v1/notes/{fake_note_id}',
            headers={
                'X-API-Key': VALID_KEY,
                'X-Session-ID': session_id,
            },
        )
        # Should be 404
        assert resp.status_code == 404

        entries = _wait_for_audit_logs(postgres_uri, expected_count=1)
        session_entries = [e for e in entries if e['session_id'] == session_id]

        # Only http.request, no domain event
        actions = {e['action'] for e in session_entries}
        assert 'http.request' in actions
        assert 'note.deleted' not in actions
