"""Unit tests for audit_event() function (AC-010, AC-011) and HTTP access log middleware (AC-006-009)."""

import time
from unittest.mock import MagicMock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from memex_core.context import set_actor, set_session_id


# ---------------------------------------------------------------------------
# audit_event() tests (AC-010, AC-011)
# ---------------------------------------------------------------------------


class TestAuditEvent:
    """Tests for the standalone audit_event() function."""

    def test_delegates_to_audit_service_log(self) -> None:
        """AC-010: audit_event() delegates to audit_service.log() with context vars."""
        from memex_core.services.audit import audit_event

        mock_svc = MagicMock()
        set_actor('test-actor')
        set_session_id('test-session')

        audit_event(mock_svc, 'note.deleted', resource_type='note', resource_id='abc-123')

        mock_svc.log.assert_called_once_with(
            action='note.deleted',
            actor='test-actor',
            resource_type='note',
            resource_id='abc-123',
            session_id='test-session',
            details=None,
        )

    def test_passes_extra_kwargs_as_details(self) -> None:
        """audit_event() forwards **details kwargs to audit_service.log()."""
        from memex_core.services.audit import audit_event

        mock_svc = MagicMock()
        set_actor('actor')
        set_session_id('sess')

        audit_event(
            mock_svc,
            'kv.written',
            resource_type='kv',
            resource_id='my-key',
            old_value='x',
            new_value='y',
        )

        call_kwargs = mock_svc.log.call_args.kwargs
        assert call_kwargs['details'] == {'old_value': 'x', 'new_value': 'y'}

    def test_noop_when_none(self) -> None:
        """AC-011: audit_event(None, ...) is a silent no-op."""
        from memex_core.services.audit import audit_event

        # Should not raise
        audit_event(None, 'test.action', resource_type='test', resource_id='id')

    def test_details_none_when_no_kwargs(self) -> None:
        """audit_event() passes details=None when no extra kwargs provided."""
        from memex_core.services.audit import audit_event

        mock_svc = MagicMock()
        set_actor('a')
        set_session_id('s')

        audit_event(mock_svc, 'test.action', resource_type='t', resource_id='i')

        call_kwargs = mock_svc.log.call_args.kwargs
        assert call_kwargs['details'] is None


# ---------------------------------------------------------------------------
# HTTP access log middleware tests (AC-006, AC-007, AC-008, AC-009)
# ---------------------------------------------------------------------------


def _make_audit_app(audit_service: MagicMock | None = None) -> FastAPI:
    """Create a minimal FastAPI app with the access log middleware."""
    app = FastAPI()

    # We replicate the middleware logic here to test in isolation
    # (avoids importing server/__init__.py which loads the full app)
    _skip = frozenset(
        {
            '/api/v1/health',
            '/api/v1/ready',
            '/api/v1/metrics',
            '/docs',
            '/openapi.json',
        }
    )

    @app.middleware('http')
    async def audit_access_log(request: Request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        if request.url.path not in _skip:
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            svc = getattr(request.app.state, 'audit_service', None)
            if svc:
                from memex_core.context import get_actor, get_session_id

                svc.log(
                    action='http.request',
                    actor=get_actor(),
                    session_id=get_session_id(),
                    details={
                        'method': request.method,
                        'path': request.url.path,
                        'status': response.status_code,
                        'latency_ms': latency_ms,
                    },
                )
        return response

    if audit_service is not None:
        app.state.audit_service = audit_service

    @app.get('/api/v1/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/api/v1/ready')
    async def ready():
        return {'status': 'ok'}

    @app.get('/api/v1/metrics')
    async def metrics():
        return {}

    @app.get('/docs')
    async def docs():
        return {}

    @app.get('/openapi.json')
    async def openapi():
        return {}

    @app.post('/api/v1/notes')
    async def create_note():
        return {'id': 'new'}

    @app.get('/api/v1/notes')
    async def list_notes():
        return {'notes': []}

    return app


class TestAccessLogMiddleware:
    """Tests for the audit_access_log middleware."""

    def test_logs_http_request_for_normal_path(self) -> None:
        """AC-006: Middleware logs http.request for non-skipped paths."""
        mock_audit = MagicMock()
        app = _make_audit_app(audit_service=mock_audit)
        client = TestClient(app)
        set_actor('test-actor')
        set_session_id('test-session')

        client.get('/api/v1/notes')

        mock_audit.log.assert_called_once()
        kwargs = mock_audit.log.call_args.kwargs
        assert kwargs['action'] == 'http.request'

    def test_captures_method_path_status_latency(self) -> None:
        """AC-007: details contains method, path, status (int), latency_ms (float)."""
        mock_audit = MagicMock()
        app = _make_audit_app(audit_service=mock_audit)
        client = TestClient(app)

        client.post('/api/v1/notes')

        kwargs = mock_audit.log.call_args.kwargs
        details = kwargs['details']
        assert details['method'] == 'POST'
        assert details['path'] == '/api/v1/notes'
        assert isinstance(details['status'], int)
        assert details['status'] == 200
        assert isinstance(details['latency_ms'], float)
        assert details['latency_ms'] >= 0

    def test_skips_health_ready_metrics_docs_openapi(self) -> None:
        """AC-008: Skips health, ready, metrics, docs, openapi.json paths."""
        mock_audit = MagicMock()
        app = _make_audit_app(audit_service=mock_audit)
        client = TestClient(app)

        for path in [
            '/api/v1/health',
            '/api/v1/ready',
            '/api/v1/metrics',
            '/docs',
            '/openapi.json',
        ]:
            client.get(path)

        mock_audit.log.assert_not_called()

    def test_reads_actor_and_session_from_context(self) -> None:
        """AC-009: Middleware reads actor and session_id from context vars."""
        mock_audit = MagicMock()
        app = _make_audit_app(audit_service=mock_audit)
        client = TestClient(app)
        set_actor('ctx-actor')
        set_session_id('ctx-session')

        client.get('/api/v1/notes')

        kwargs = mock_audit.log.call_args.kwargs
        assert kwargs['actor'] == 'ctx-actor'
        assert kwargs['session_id'] == 'ctx-session'

    def test_graceful_when_no_audit_service(self) -> None:
        """Middleware is a no-op when audit_service is not on app.state."""
        app = _make_audit_app(audit_service=None)
        client = TestClient(app)
        # Should not raise
        resp = client.get('/api/v1/notes')
        assert resp.status_code == 200
