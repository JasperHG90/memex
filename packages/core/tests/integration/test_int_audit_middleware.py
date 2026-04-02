"""Integration tests for HTTP access log middleware (AC-006, AC-007, AC-008, AC-009).

These tests exercise the real middleware stack against a real Postgres
instance via testcontainers, validating that audit log entries are
persisted correctly.
"""

import asyncio
import json
import os
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


@pytest.fixture(autouse=True)
def clean_audit_logs(postgres_uri: str) -> Generator[None, None, None]:
    """Truncate audit_logs before each test."""
    _clear_audit_logs(postgres_uri)
    yield


@pytest.mark.integration
class TestAccessLogMiddlewareIntegration:
    """Integration tests for the audit_access_log middleware."""

    def test_logs_http_request_for_api_endpoint(
        self, audit_client: TestClient, postgres_uri: str
    ) -> None:
        """AC-006: GET to an API endpoint produces an http.request audit entry."""
        resp = audit_client.get('/api/v1/vaults', headers={'X-API-Key': VALID_KEY})
        assert resp.status_code == 200

        entries = _query_audit_logs(postgres_uri, action='http.request')
        assert len(entries) >= 1
        entry = entries[0]
        assert entry['action'] == 'http.request'

    def test_details_contain_method_path_status_latency(
        self, audit_client: TestClient, postgres_uri: str
    ) -> None:
        """AC-007: details JSONB has method, path, status (int), latency_ms (float)."""
        audit_client.get('/api/v1/vaults', headers={'X-API-Key': VALID_KEY})

        entries = _query_audit_logs(postgres_uri, action='http.request')
        assert len(entries) >= 1
        details = entries[0]['details']
        assert details['method'] == 'GET'
        assert details['path'] == '/api/v1/vaults'
        assert isinstance(details['status'], int)
        assert details['status'] == 200
        assert isinstance(details['latency_ms'], (int, float))
        assert details['latency_ms'] >= 0

    def test_skips_health_ready_metrics(self, audit_client: TestClient, postgres_uri: str) -> None:
        """AC-008: No http.request entries for skipped paths."""
        skip_paths = ['/api/v1/health', '/api/v1/ready', '/api/v1/metrics']
        for path in skip_paths:
            audit_client.get(path)

        entries = _query_audit_logs(postgres_uri, action='http.request')
        skipped_entries = [e for e in entries if e['details']['path'] in skip_paths]
        assert len(skipped_entries) == 0

    def test_captures_actor_and_session_from_context(
        self, audit_client: TestClient, postgres_uri: str
    ) -> None:
        """AC-009: Authenticated request with X-Session-ID carries actor and session_id."""
        custom_session = 'integration-test-session-42'
        audit_client.get(
            '/api/v1/vaults',
            headers={
                'X-API-Key': VALID_KEY,
                'X-Session-ID': custom_session,
            },
        )

        entries = _query_audit_logs(postgres_uri, action='http.request', session_id=custom_session)
        assert len(entries) == 1
        entry = entries[0]
        assert entry['session_id'] == custom_session
        assert entry['actor'] == f'{KEY_DESCRIPTION} ({KEY_PREFIX})'
