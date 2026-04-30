"""F32 Prometheus metrics — integration test (Test 6)."""

from __future__ import annotations

import os
from typing import Generator
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer


def _build_env_vars(container: PostgresContainer, tmp_path) -> dict[str, str]:
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
        'MEMEX_SERVER__FILE_STORE__TYPE': 'local',
        'MEMEX_SERVER__FILE_STORE__ROOT': str(tmp_path),
    }


@pytest.fixture
def metrics_client(
    postgres_container: PostgresContainer,
    tmp_path,
) -> Generator[TestClient, None, None]:
    env = _build_env_vars(postgres_container, tmp_path)
    from memex_core.server import app

    with patch.dict(os.environ, env):
        with TestClient(app) as c:
            yield c


@pytest.mark.integration
def test_prometheus_counters_emitted(metrics_client: TestClient):
    """The three F32 metrics are registered and exposed at /api/v1/metrics."""
    # Touch the module so the collectors register against the global registry,
    # even before any compute fires.
    import memex_core.metrics  # noqa: F401

    resp = metrics_client.get('/api/v1/metrics')
    assert resp.status_code == 200
    body = resp.text

    # Each metric has at least a HELP line registered, even with zero observations.
    assert 'memex_diagnostics_manifold_compute_seconds' in body
    assert 'memex_diagnostics_cache_hits_total' in body
    assert 'memex_diagnostics_cache_misses_total' in body
