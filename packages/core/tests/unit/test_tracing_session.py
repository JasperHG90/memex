"""Tests that session IDs are propagated to OpenTelemetry spans via openinference."""

from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from fastapi.testclient import TestClient


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
        mock_metastore = MagicMock()
        mock_metastore.connect = AsyncMock()
        mock_metastore.close = AsyncMock()
        mock_meta.return_value = mock_metastore

        mock_filestore = MagicMock()
        mock_filestore.check_connection = AsyncMock(return_value=True)
        mock_fs.return_value = mock_filestore

        mock_api = MagicMock()
        mock_api.initialize = AsyncMock()
        mock_api.resolve_vault_identifier = AsyncMock(return_value='test-vault-id')
        mock_api_cls.return_value = mock_api

        config_mock = MagicMock()
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


_test_endpoint_added = False


@pytest.fixture
def client():
    from memex_core.server import app

    global _test_endpoint_added
    if not _test_endpoint_added:

        @app.get('/test-tracing-session')
        def test_tracing_session_endpoint():
            from memex_core.context import get_session_id

            return {'session_id': get_session_id()}

        _test_endpoint_added = True

    with TestClient(app) as c:
        yield c


def test_middleware_calls_using_session_when_available(client):
    """When openinference is installed, middleware wraps call_next with using_session."""
    mock_using_session_instance = MagicMock()
    mock_using_session_instance.__enter__ = MagicMock(return_value=mock_using_session_instance)
    mock_using_session_instance.__exit__ = MagicMock(return_value=False)
    mock_using_session_cls = MagicMock(return_value=mock_using_session_instance)

    with patch('memex_core.server._oi_using_session', mock_using_session_cls):
        response = client.get('/test-tracing-session')
        assert response.status_code == 200

    session_id = response.headers['X-Session-ID']
    mock_using_session_cls.assert_called_with(session_id)
    mock_using_session_instance.__enter__.assert_called()
    mock_using_session_instance.__exit__.assert_called()


def test_middleware_works_without_openinference(client):
    """When openinference is not installed, middleware still works normally."""
    with patch('memex_core.server._oi_using_session', None):
        response = client.get('/test-tracing-session')
        assert response.status_code == 200
        assert 'X-Session-ID' in response.headers
        assert response.json()['session_id'] != 'global'


def test_middleware_passes_custom_session_id_to_using_session(client):
    """Custom X-Session-ID header value is forwarded to using_session."""
    mock_using_session_instance = MagicMock()
    mock_using_session_instance.__enter__ = MagicMock(return_value=mock_using_session_instance)
    mock_using_session_instance.__exit__ = MagicMock(return_value=False)
    mock_using_session_cls = MagicMock(return_value=mock_using_session_instance)

    custom_sid = 'my-custom-session-42'

    with patch('memex_core.server._oi_using_session', mock_using_session_cls):
        response = client.get('/test-tracing-session', headers={'X-Session-ID': custom_sid})
        assert response.status_code == 200

    mock_using_session_cls.assert_called_with(custom_sid)
    assert response.headers['X-Session-ID'] == custom_sid


def test_using_session_sets_otel_context():
    """Integration test: verify using_session actually sets session.id in OTel context."""
    try:
        from openinference.instrumentation import using_session
        from openinference.semconv.trace import SpanAttributes
        from opentelemetry.context import get_current, get_value
    except ImportError:
        pytest.skip('openinference/opentelemetry not installed')

    sid = 'integration-test-session-99'
    with using_session(sid):
        ctx = get_current()
        value = get_value(SpanAttributes.SESSION_ID, ctx)
        assert value == sid

    # Outside the context manager, value should be gone
    ctx = get_current()
    value = get_value(SpanAttributes.SESSION_ID, ctx)
    assert value is None
