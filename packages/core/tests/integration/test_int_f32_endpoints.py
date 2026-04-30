"""F32 HTTP endpoints — integration tests (Tests 4b, 5).

- 4b: GET /diagnostics/retrieval/{vault_id} returns heatmap JSON.
- 5:  Endpoints return 501 when umap-learn is unavailable.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Generator
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import UUID, uuid4

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
def f32_client(
    postgres_container: PostgresContainer,
    tmp_path,
) -> Generator[TestClient, None, None]:
    """TestClient with the full lifespan, auth disabled, FileStore in tmp_path."""
    env = _build_env_vars(postgres_container, tmp_path)
    from memex_core.server import app

    with patch.dict(os.environ, env):
        with TestClient(app) as c:
            yield c


def _seed_vault_via_api(client: TestClient) -> UUID:
    """Create a vault via the running API and return its id."""
    resp = client.post('/api/v1/vaults', json={'name': f'F32-ep-{uuid4().hex[:8]}'})
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    return UUID(body['id'])


@pytest.mark.integration
def test_retrieval_endpoint_returns_heatmap_json(f32_client: TestClient):
    """GET /api/v1/diagnostics/retrieval/{vault_id} returns 200 with heatmap shape."""
    vault_id = _seed_vault_via_api(f32_client)
    resp = f32_client.get(f'/api/v1/diagnostics/retrieval/{vault_id}')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['vault_id'] == str(vault_id)
    assert 'as_of' in body
    assert body['top_n'] == 50
    assert isinstance(body['entities'], list)


@pytest.mark.integration
def test_endpoints_return_501_when_umap_unavailable(
    postgres_container: PostgresContainer,
    tmp_path,
):
    """Patch the umap import to fail; GET /diagnostics/manifold/... → 501."""
    env = _build_env_vars(postgres_container, tmp_path)
    from memex_core.server import app

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == 'umap':
            raise ImportError('umap-learn not installed')
        return real_import(name, *args, **kwargs)

    # Pre-pop any cached umap module so the import is forced.
    sys.modules.pop('umap', None)

    with patch.dict(os.environ, env):
        with patch('builtins.__import__', side_effect=fake_import):
            with TestClient(app) as c:
                resp = c.post('/api/v1/vaults', json={'name': f'F32-501-{uuid4().hex[:8]}'})
                assert resp.status_code in (200, 201), resp.text
                vault_id = UUID(resp.json()['id'])

                resp2 = c.get(f'/api/v1/diagnostics/manifold/{vault_id}')
                assert resp2.status_code == 501, resp2.text
                assert 'umap-learn' in json.dumps(resp2.json()).lower()
