"""Tests for health check endpoints."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from memex_core.server import app


@pytest.fixture
def client():
    """TestClient with mocked app state for health endpoints."""
    # Health endpoints access app.state.api directly (no DI override needed
    # for /health since it has no dependencies, but /ready needs metastore).
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()

    mock_metastore = MagicMock()
    mock_metastore.session = MagicMock(return_value=mock_session)

    mock_filestore = MagicMock()
    mock_filestore.check_connection = AsyncMock(return_value=True)

    mock_tracing = SimpleNamespace(enabled=False)
    mock_server_config = SimpleNamespace(tracing=mock_tracing)
    mock_config = SimpleNamespace(server=mock_server_config)

    mock_api = SimpleNamespace(
        metastore=mock_metastore, filestore=mock_filestore, config=mock_config
    )
    app.state.api = mock_api

    yield TestClient(app)

    # Clean up
    if hasattr(app.state, 'api'):
        del app.state.api


class TestHealthEndpoint:
    """Tests for GET /api/v1/health (liveness probe)."""

    def test_health_returns_200(self, client):
        response = client.get('/api/v1/health')
        assert response.status_code == 200
        assert response.json() == {'status': 'ok'}

    def test_health_is_get_only(self, client):
        response = client.post('/api/v1/health')
        assert response.status_code == 405


class TestReadyEndpoint:
    """Tests for GET /api/v1/ready (readiness probe)."""

    def test_ready_returns_200_when_db_reachable(self, client):
        response = client.get('/api/v1/ready')
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'ok'
        assert data['database'] == 'ok'
        assert data['filestore'] == 'ok'

    def test_ready_returns_503_when_db_unreachable(self, client):
        # Make the connection's execute raise an exception
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=ConnectionError('DB down'))

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.connection = AsyncMock(return_value=mock_conn)

        app.state.api.metastore.session = MagicMock(return_value=mock_session)

        response = client.get('/api/v1/ready')
        assert response.status_code == 503
        data = response.json()
        assert data['status'] == 'unavailable'
        assert data['database'] == 'unavailable'

    def test_ready_returns_503_on_session_failure(self, client):
        # Make opening the session itself fail
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=RuntimeError('Pool exhausted'))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        app.state.api.metastore.session = MagicMock(return_value=mock_session)

        response = client.get('/api/v1/ready')
        assert response.status_code == 503
        data = response.json()
        assert data['status'] == 'unavailable'
        assert data['database'] == 'unavailable'

    def test_ready_is_get_only(self, client):
        response = client.post('/api/v1/ready')
        assert response.status_code == 405
