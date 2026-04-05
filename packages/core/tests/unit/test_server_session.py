import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch, AsyncMock
from memex_core.server import app


# We need to mock the lifespan to avoid DB connections
@pytest.fixture(autouse=True)
def mock_lifespan_dependencies():
    with (
        patch('memex_core.server.get_metastore') as mock_meta,
        patch('memex_core.server.get_filestore') as mock_fs,
        patch('memex_core.server.run_scheduler_with_leader_election'),
        patch('memex_core.server.parse_memex_config') as mock_conf,
        patch('memex_core.server.setup_auth'),
        patch('memex_core.server.setup_rate_limiting'),
        patch('memex_core.server.get_embedding_model', new_callable=AsyncMock),
        patch('memex_core.server.get_reranking_model', new_callable=AsyncMock),
        patch('memex_core.server.get_ner_model', new_callable=AsyncMock),
        patch('memex_core.server.MemexAPI') as mock_api_cls,
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

        # Configure filestore mock
        mock_filestore = MagicMock()
        mock_filestore.check_connection = AsyncMock(return_value=True)
        mock_fs.return_value = mock_filestore

        # Configure MemexAPI mock
        mock_api = MagicMock()
        mock_api.initialize = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value='test-vault-id')
        mock_api_cls.return_value = mock_api

        # Configure Config Mock
        config_mock = MagicMock()
        # Ensure deep nesting exists
        config_mock.server.memory.extraction.model.model = 'gemini-1.5-flash'
        config_mock.server.memory.extraction.max_concurrency = 5
        config_mock.server.default_active_vault = 'global'
        config_mock.server.default_reader_vault = 'global'
        config_mock.server.logging.level = 'WARNING'
        config_mock.server.logging.json_output = False

        config_mock.server.cache_dir = '/tmp/memex-test-cache'
        config_mock.server.tracing.enabled = False
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


class TestSessionBriefingEndpoint:
    """Tests for GET /api/v1/vaults/{vault_id}/session-briefing."""

    def test_invalid_budget_returns_422(self, client_with_mock_db):
        """Budget values other than 1000/2000 are rejected with 422."""
        from uuid import uuid4

        vault_id = str(uuid4())
        response = client_with_mock_db.get(
            f'/api/v1/vaults/{vault_id}/session-briefing',
            params={'budget': 500},
        )
        assert response.status_code == 422
        assert 'budget' in response.json()['detail'].lower()

    def test_zero_budget_returns_422(self, client_with_mock_db):
        """Budget=0 is rejected."""
        from uuid import uuid4

        vault_id = str(uuid4())
        response = client_with_mock_db.get(
            f'/api/v1/vaults/{vault_id}/session-briefing',
            params={'budget': 0},
        )
        assert response.status_code == 422
