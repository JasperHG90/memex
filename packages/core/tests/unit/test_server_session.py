import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch, AsyncMock
from memex_core.server import app


# We need to mock the lifespan to avoid DB connections
@pytest.fixture(autouse=True)
def mock_lifespan_dependencies():
    with (
        patch('memex_core.server.get_metastore') as mock_meta,
        patch('memex_core.server.get_filestore'),
        patch('memex_core.server.run_scheduler_with_leader_election'),
        patch('memex_core.server.parse_memex_config') as mock_conf,
    ):
        # Configure metastore mock
        mock_metastore = MagicMock()
        mock_metastore.connect = AsyncMock()
        mock_metastore.close = AsyncMock()

        # Configure session mock
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.delete = MagicMock()

        # Mock result of session.exec(stmt).all()
        # (await session.exec(stmt)) -> mock_result
        # mock_result.all() -> [] (list)
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_result.first.return_value = None
        mock_session.exec.return_value = mock_result
        mock_session.get.return_value = None  # For Vault lookup by ID

        # Async context manager for session()
        mock_metastore.session.return_value.__aenter__.return_value = mock_session

        mock_meta.return_value = mock_metastore

        # Configure Config Mock
        config_mock = MagicMock()
        # Ensure deep nesting exists
        config_mock.server.memory.extraction.model.model = 'gemini-1.5-flash'
        config_mock.server.memory.extraction.max_concurrency = 5
        config_mock.server.memory.opinion_formation.confidence.similarity_threshold = 0.8
        config_mock.server.active_vault = 'global'
        config_mock.server.attached_vaults = []

        config_mock.server.meta_store = MagicMock()
        config_mock.server.file_store = MagicMock()
        mock_conf.return_value = config_mock

        yield


@pytest.fixture
def client_with_mock_db():
    # Define a simple route on the app for testing
    # Note: We can't easily remove routes from FastAPI, so this route persists across tests if not careful.
    # But for unit tests it's usually fine.
    @app.get('/test-session')
    def test_session_endpoint():
        from memex_core.context import get_session_id

        return {'session_id': get_session_id()}

    with TestClient(app) as c:
        yield c


def test_middleware_generates_session_id(client_with_mock_db):
    response = client_with_mock_db.get('/test-session')
    assert response.status_code == 200
    assert 'X-Session-ID' in response.headers
    # Should be a UUID
    assert len(response.headers['X-Session-ID']) > 10
    assert response.json()['session_id'] != 'global'
    assert response.json()['session_id'] == response.headers['X-Session-ID']


def test_middleware_respects_header(client_with_mock_db):
    custom_id = 'test-session-123'
    response = client_with_mock_db.get('/test-session', headers={'X-Session-ID': custom_id})
    assert response.status_code == 200
    assert response.headers['X-Session-ID'] == custom_id
    assert response.json()['session_id'] == custom_id
