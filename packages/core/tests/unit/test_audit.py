"""Tests for audit logging service and endpoints."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from memex_common.config import ApiKeyConfig, AuthConfig, Policy
from memex_core.memory.sql_models import AuditLog
from memex_core.services.audit import AuditService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_metastore():
    """A mock metastore with a session context manager."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    metastore = MagicMock()
    metastore.session = MagicMock(return_value=mock_session)

    return metastore, mock_session


@pytest.fixture()
def audit_service(mock_metastore):
    metastore, _ = mock_metastore
    return AuditService(metastore)


# ---------------------------------------------------------------------------
# AuditLog model tests
# ---------------------------------------------------------------------------


class TestAuditLogModel:
    """Tests for the AuditLog SQLModel."""

    def test_create_audit_log(self):
        entry = AuditLog(
            action='auth.success',
            actor='test-key...',
            resource_type='note',
            resource_id=str(uuid4()),
            session_id='sess-123',
            details={'path': '/api/v1/notes'},
        )
        assert entry.action == 'auth.success'
        assert entry.actor == 'test-key...'
        assert entry.id is not None
        assert entry.timestamp is not None

    def test_minimal_audit_log(self):
        entry = AuditLog(action='auth.failure')
        assert entry.action == 'auth.failure'
        assert entry.actor is None
        assert entry.resource_type is None
        assert entry.details is None


# ---------------------------------------------------------------------------
# AuditService tests
# ---------------------------------------------------------------------------


class TestAuditServiceLog:
    """Tests for AuditService.log (fire-and-forget)."""

    def test_log_creates_task(self, audit_service, mock_metastore):
        """log() should schedule a background task."""
        with patch('asyncio.create_task') as mock_create_task:
            audit_service.log(action='auth.success', actor='key...')
            mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_persist_writes_to_session(self, audit_service, mock_metastore):
        """_persist should add and commit the entry."""
        _, mock_session = mock_metastore
        entry = AuditLog(action='test.action')

        await audit_service._persist(entry)

        mock_session.add.assert_called_once_with(entry)
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_persist_handles_error_gracefully(self, audit_service, mock_metastore):
        """_persist should not raise on DB errors."""
        _, mock_session = mock_metastore
        mock_session.commit.side_effect = RuntimeError('DB down')

        entry = AuditLog(action='test.action')
        # Should not raise
        await audit_service._persist(entry)


# ---------------------------------------------------------------------------
# AuditService.query tests
# ---------------------------------------------------------------------------


class TestAuditServiceQuery:
    """Tests for AuditService.query."""

    @pytest.mark.asyncio
    async def test_query_executes(self, audit_service, mock_metastore):
        _, mock_session = mock_metastore
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_result)

        result = await audit_service.query(action='auth.success')
        assert result == []
        mock_session.exec.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_with_all_filters(self, audit_service, mock_metastore):
        _, mock_session = mock_metastore
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_result)

        now = datetime.now(timezone.utc)
        result = await audit_service.query(
            actor='key...',
            action='auth.success',
            resource_type='note',
            since=now,
            until=now,
            limit=10,
            offset=5,
        )
        assert result == []


# ---------------------------------------------------------------------------
# Audit endpoint tests
# ---------------------------------------------------------------------------


def _build_audit_app(audit_service: AuditService) -> FastAPI:
    """Build a minimal FastAPI app with the audit endpoint (no full server import)."""
    from datetime import datetime
    from typing import Annotated, Any
    from uuid import UUID

    from fastapi import Query, Request
    from pydantic import BaseModel

    app = FastAPI()
    app.state.audit_service = audit_service

    class AuditEntryDTO(BaseModel):
        id: UUID
        timestamp: datetime
        actor: str | None
        action: str
        resource_type: str | None
        resource_id: str | None
        session_id: str | None
        details: dict[str, Any] | None

    @app.get('/api/v1/admin/audit', response_model=list[AuditEntryDTO])
    async def list_audit_entries(
        request: Request,
        actor: Annotated[str | None, Query()] = None,
        action: Annotated[str | None, Query()] = None,
        resource_type: Annotated[str | None, Query()] = None,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[AuditEntryDTO]:
        svc: AuditService = request.app.state.audit_service
        entries = await svc.query(
            actor=actor,
            action=action,
            resource_type=resource_type,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return [
            AuditEntryDTO(
                id=e.id,
                timestamp=e.timestamp,
                actor=e.actor,
                action=e.action,
                resource_type=e.resource_type,
                resource_id=e.resource_id,
                session_id=e.session_id,
                details=e.details,
            )
            for e in entries
        ]

    return app


class TestAuditEndpoint:
    """Tests for GET /api/v1/admin/audit."""

    @pytest.fixture()
    def client(self, audit_service):
        app = _build_audit_app(audit_service)
        return TestClient(app)

    def test_list_audit_empty(self, client, mock_metastore):
        _, mock_session = mock_metastore
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_result)

        response = client.get('/api/v1/admin/audit')
        assert response.status_code == 200
        assert response.json() == []

    def test_list_audit_with_entries(self, client, mock_metastore):
        _, mock_session = mock_metastore
        entry = AuditLog(
            action='auth.success',
            actor='key...',
            details={'path': '/api/v1/notes'},
        )
        mock_result = MagicMock()
        mock_result.all.return_value = [entry]
        mock_session.exec = AsyncMock(return_value=mock_result)

        response = client.get('/api/v1/admin/audit')
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]['action'] == 'auth.success'
        assert data[0]['actor'] == 'key...'

    def test_list_audit_with_filters(self, client, mock_metastore):
        _, mock_session = mock_metastore
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec = AsyncMock(return_value=mock_result)

        response = client.get(
            '/api/v1/admin/audit',
            params={'action': 'auth.failure', 'limit': 10},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Auth middleware audit integration
# ---------------------------------------------------------------------------


def _import_setup_auth():
    """Import setup_auth without triggering server.__init__.py."""
    import importlib.util
    import pathlib as plb

    import memex_core

    auth_path = plb.Path(memex_core.__file__).resolve().parent / 'server' / 'auth.py'
    spec = importlib.util.spec_from_file_location('memex_core_server_auth', auth_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules so @dataclass can resolve __module__
    import sys

    sys.modules['memex_core_server_auth'] = module
    spec.loader.exec_module(module)
    return module.setup_auth, module.auth_middleware


class TestAuthAuditIntegration:
    """Tests that auth middleware logs audit events."""

    def _make_app_with_audit(self, auth_config, audit_service):
        setup_auth, auth_mw = _import_setup_auth()

        app = FastAPI()
        app.middleware('http')(auth_mw)
        app.state.audit_service = audit_service
        setup_auth(app, auth_config)

        @app.get('/api/v1/notes')
        async def notes():
            return {'notes': []}

        @app.get('/api/v1/health')
        async def health():
            return {'status': 'ok'}

        return app

    def test_auth_success_no_longer_logged(self, audit_service):
        """auth.success is no longer emitted — replaced by Layer 1 access log."""
        config = AuthConfig(
            enabled=True,
            keys=[ApiKeyConfig(key=SecretStr('test-key-123'), policy=Policy.ADMIN)],
        )
        app = self._make_app_with_audit(config, audit_service)
        client = TestClient(app)

        with patch.object(audit_service, 'log') as mock_log:
            client.get('/api/v1/notes', headers={'X-API-Key': 'test-key-123'})
            # auth.success should NOT be emitted (replaced by access log middleware)
            for call in mock_log.call_args_list:
                assert call.kwargs.get('action') != 'auth.success'

    def test_auth_failure_logs_audit(self, audit_service):
        config = AuthConfig(
            enabled=True,
            keys=[ApiKeyConfig(key=SecretStr('test-key-123'), policy=Policy.ADMIN)],
        )
        app = self._make_app_with_audit(config, audit_service)
        client = TestClient(app)

        with patch.object(audit_service, 'log') as mock_log:
            client.get('/api/v1/notes', headers={'X-API-Key': 'wrong'})
            mock_log.assert_called_once()
            assert mock_log.call_args.kwargs['action'] == 'auth.failure'

    def test_auth_missing_key_logs_audit(self, audit_service):
        config = AuthConfig(
            enabled=True,
            keys=[ApiKeyConfig(key=SecretStr('test-key-123'), policy=Policy.ADMIN)],
        )
        app = self._make_app_with_audit(config, audit_service)
        client = TestClient(app)

        with patch.object(audit_service, 'log') as mock_log:
            client.get('/api/v1/notes')
            mock_log.assert_called_once()
            assert mock_log.call_args.kwargs['action'] == 'auth.missing_key'

    def test_exempt_path_no_audit(self, audit_service):
        config = AuthConfig(
            enabled=True,
            keys=[ApiKeyConfig(key=SecretStr('test-key-123'), policy=Policy.ADMIN)],
        )
        app = self._make_app_with_audit(config, audit_service)
        client = TestClient(app)

        with patch.object(audit_service, 'log') as mock_log:
            client.get('/api/v1/health')
            mock_log.assert_not_called()
