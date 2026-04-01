"""Unit tests for auth middleware audit changes (AC-003, AC-004, AC-005)."""

import importlib.util
import pathlib as plb
import sys
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from memex_common.config import ApiKeyConfig, AuthConfig, Policy

import memex_core

# Import auth module directly to avoid triggering server/__init__.py
_auth_path = plb.Path(memex_core.__file__).resolve().parent / 'server' / 'auth.py'
_spec = importlib.util.spec_from_file_location('_auth_audit_test', _auth_path)
assert _spec is not None and _spec.loader is not None
_auth_mod = importlib.util.module_from_spec(_spec)
sys.modules['_auth_audit_test'] = _auth_mod
_spec.loader.exec_module(_auth_mod)
setup_auth = _auth_mod.setup_auth
auth_middleware = _auth_mod.auth_middleware

VALID_KEY = 'test-key-abc123456'
KEY_PREFIX = VALID_KEY[:8] + '...'


def _key(
    secret: str, policy: Policy = Policy.ADMIN, description: str | None = None
) -> ApiKeyConfig:
    return ApiKeyConfig(key=SecretStr(secret), policy=policy, description=description)


def _make_app(auth_config: AuthConfig, audit_service: MagicMock | None = None) -> FastAPI:
    app = FastAPI()
    app.middleware('http')(auth_middleware)
    setup_auth(app, auth_config)
    if audit_service is not None:
        app.state.audit_service = audit_service

    @app.get('/api/v1/notes')
    async def notes():
        return {'notes': []}

    return app


class TestAuthMiddlewareSetsActor:
    """AC-003: Auth middleware calls set_actor() after resolving the API key."""

    def test_actor_set_on_valid_key(self) -> None:
        """Valid API key sets actor in context."""
        from memex_core.context import get_actor

        auth_config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        captured_actor: str | None = None

        app = FastAPI()
        app.middleware('http')(auth_middleware)
        setup_auth(app, auth_config)

        @app.get('/api/v1/test')
        async def test_route():
            nonlocal captured_actor
            captured_actor = get_actor()
            return {'ok': True}

        client = TestClient(app)
        client.get('/api/v1/test', headers={'X-API-Key': VALID_KEY})
        assert captured_actor == KEY_PREFIX

    def test_actor_includes_description_when_present(self) -> None:
        """Actor string includes key description when available."""
        from memex_core.context import get_actor

        auth_config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, description='my-agent')])
        captured_actor: str | None = None

        app = FastAPI()
        app.middleware('http')(auth_middleware)
        setup_auth(app, auth_config)

        @app.get('/api/v1/test')
        async def test_route():
            nonlocal captured_actor
            captured_actor = get_actor()
            return {'ok': True}

        client = TestClient(app)
        client.get('/api/v1/test', headers={'X-API-Key': VALID_KEY})
        assert captured_actor == f'my-agent ({KEY_PREFIX})'


class TestAuthSuccessRemoved:
    """AC-004: Auth middleware no longer emits auth.success audit log."""

    def test_no_auth_success_logged(self) -> None:
        """Valid API key does NOT produce auth.success audit entry."""
        mock_audit = MagicMock()
        auth_config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(auth_config, audit_service=mock_audit)
        client = TestClient(app)
        client.get('/api/v1/notes', headers={'X-API-Key': VALID_KEY})

        # Ensure .log() was never called with action='auth.success'
        for call in mock_audit.log.call_args_list:
            assert call.kwargs.get('action') != 'auth.success', (
                'auth.success should no longer be emitted by auth middleware'
            )


class TestAuthFailureStillLogged:
    """AC-005: Auth middleware still emits auth.failure and auth.missing_key."""

    def test_missing_key_still_logged(self) -> None:
        """Request without API key logs auth.missing_key."""
        mock_audit = MagicMock()
        auth_config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(auth_config, audit_service=mock_audit)
        client = TestClient(app)
        resp = client.get('/api/v1/notes')
        assert resp.status_code == 401

        mock_audit.log.assert_called_once()
        assert mock_audit.log.call_args.kwargs['action'] == 'auth.missing_key'

    def test_invalid_key_still_logged(self) -> None:
        """Request with invalid API key logs auth.failure."""
        mock_audit = MagicMock()
        auth_config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(auth_config, audit_service=mock_audit)
        client = TestClient(app)
        resp = client.get('/api/v1/notes', headers={'X-API-Key': 'wrong-key-12345'})
        assert resp.status_code == 403

        mock_audit.log.assert_called_once()
        assert mock_audit.log.call_args.kwargs['action'] == 'auth.failure'
