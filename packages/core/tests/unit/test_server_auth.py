"""Tests for API key authentication middleware."""

import importlib.util
import pathlib as plb

import pytest

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from memex_common.config import ApiKeyConfig, AuthConfig, Permission, Policy, POLICY_PERMISSIONS

# Import auth module directly to avoid triggering server/__init__.py
# (which imports the full MemexAPI → retrieval chain).
import sys

import memex_core

_auth_path = plb.Path(memex_core.__file__).resolve().parent / 'server' / 'auth.py'
_spec = importlib.util.spec_from_file_location('_auth', _auth_path)
assert _spec is not None and _spec.loader is not None
_auth_mod = importlib.util.module_from_spec(_spec)
# Register in sys.modules so @dataclass can resolve __module__
sys.modules['_auth'] = _auth_mod
_spec.loader.exec_module(_auth_mod)
setup_auth = _auth_mod.setup_auth
auth_middleware = _auth_mod.auth_middleware
require_admin_auth = _auth_mod.require_admin_auth
_validate_key = _auth_mod._validate_key
_resolve_key = _auth_mod._resolve_key
AuthContext = _auth_mod.AuthContext
get_auth_context = _auth_mod.get_auth_context
require_read = _auth_mod.require_read
require_write = _auth_mod.require_write
require_delete = _auth_mod.require_delete


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(auth_config: AuthConfig) -> FastAPI:
    """Create a minimal FastAPI app with auth middleware and a test endpoint."""
    app = FastAPI()
    # Register the middleware at app creation time (before startup), then
    # call setup_auth to store config on app.state.
    app.middleware('http')(auth_middleware)
    setup_auth(app, auth_config)

    @app.get('/api/v1/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/api/v1/ready')
    async def ready():
        return {'status': 'ok'}

    @app.get('/api/v1/metrics')
    async def metrics():
        return {'metrics': []}

    @app.get('/api/v1/notes')
    async def notes():
        return {'notes': []}

    @app.post('/api/v1/ingestions/text')
    async def ingest():
        return {'id': 'abc'}

    return app


VALID_KEY = 'test-key-abc123'
SECOND_KEY = 'test-key-xyz789'


def _key(secret: str, policy: Policy = Policy.ADMIN) -> ApiKeyConfig:
    """Shorthand for creating an ApiKeyConfig."""
    return ApiKeyConfig(key=SecretStr(secret), policy=policy)


# ---------------------------------------------------------------------------
# AuthConfig tests
# ---------------------------------------------------------------------------


class TestAuthConfig:
    """Tests for the AuthConfig model defaults and validation."""

    def test_disabled_by_default(self):
        config = AuthConfig()
        assert config.enabled is False
        assert config.keys == []

    def test_default_exempt_paths(self):
        config = AuthConfig()
        assert '/api/v1/health' in config.exempt_paths
        assert '/api/v1/ready' in config.exempt_paths
        assert '/api/v1/metrics' in config.exempt_paths

    def test_custom_exempt_paths(self):
        config = AuthConfig(exempt_paths=['/custom'])
        assert config.exempt_paths == ['/custom']

    def test_keys_stored_as_secret(self):
        config = AuthConfig(keys=[_key('my-key')])
        assert config.keys[0].key.get_secret_value() == 'my-key'
        assert config.keys[0].policy == Policy.ADMIN

    def test_legacy_api_keys_rejected(self):
        with pytest.raises(ValueError, match='api_keys.*replaced.*keys'):
            AuthConfig(api_keys=[SecretStr('old-key')])

    def test_env_key_resolution(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {'MY_TEST_KEY': 'resolved-secret'}):
            config = AuthConfig(keys=[{'key': 'env:MY_TEST_KEY', 'policy': 'admin'}])
            assert config.keys[0].key.get_secret_value() == 'resolved-secret'

    def test_env_key_missing_raises(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NONEXISTENT_KEY', None)
            with pytest.raises(ValueError, match='NONEXISTENT_KEY.*not set'):
                ApiKeyConfig(key='env:NONEXISTENT_KEY', policy='admin')

    def test_read_vault_ids_requires_vault_ids(self):
        """read_vault_ids cannot be set when vault_ids is None."""
        with pytest.raises(ValueError, match='read_vault_ids cannot be set'):
            ApiKeyConfig(
                key=SecretStr('test-key'),
                policy=Policy.WRITER,
                vault_ids=None,
                read_vault_ids=['vault-b'],
            )

    def test_read_vault_ids_allowed_with_vault_ids(self):
        """read_vault_ids is valid when vault_ids is set."""
        config = ApiKeyConfig(
            key=SecretStr('test-key'),
            policy=Policy.WRITER,
            vault_ids=['vault-a'],
            read_vault_ids=['vault-b'],
        )
        assert config.read_vault_ids == ['vault-b']

    def test_read_vault_ids_none_by_default(self):
        """read_vault_ids defaults to None."""
        config = ApiKeyConfig(
            key=SecretStr('test-key'),
            policy=Policy.WRITER,
        )
        assert config.read_vault_ids is None


# ---------------------------------------------------------------------------
# _validate_key tests
# ---------------------------------------------------------------------------


class TestValidateKey:
    """Tests for the _validate_key helper function."""

    def test_valid_key_returns_true(self):
        config = AuthConfig(keys=[_key(VALID_KEY)])
        assert _validate_key(VALID_KEY, config) is True

    def test_invalid_key_returns_false(self):
        config = AuthConfig(keys=[_key(VALID_KEY)])
        assert _validate_key('wrong-key', config) is False

    def test_empty_keys_always_false(self):
        config = AuthConfig(keys=[])
        assert _validate_key('any-key', config) is False

    def test_multiple_keys_any_match(self):
        config = AuthConfig(keys=[_key(VALID_KEY), _key(SECOND_KEY)])
        assert _validate_key(VALID_KEY, config) is True
        assert _validate_key(SECOND_KEY, config) is True
        assert _validate_key('wrong', config) is False


# ---------------------------------------------------------------------------
# Middleware disabled tests
# ---------------------------------------------------------------------------


class TestAuthDisabled:
    """When auth is disabled, all requests should pass through."""

    def test_no_key_required(self):
        app = _make_app(AuthConfig(enabled=False))
        client = TestClient(app)
        response = client.get('/api/v1/notes')
        assert response.status_code == 200

    def test_health_accessible(self):
        app = _make_app(AuthConfig(enabled=False))
        client = TestClient(app)
        response = client.get('/api/v1/health')
        assert response.status_code == 200

    def test_no_auth_config_on_app_state(self):
        """When disabled, setup_auth should not store config on app.state."""
        app = FastAPI()
        setup_auth(app, AuthConfig(enabled=False))
        assert not hasattr(app.state, 'auth_config')


# ---------------------------------------------------------------------------
# Middleware enabled tests
# ---------------------------------------------------------------------------


class TestAuthEnabled:
    """When auth is enabled, requests must carry a valid X-API-Key header."""

    @pytest.fixture()
    def client(self):
        config = AuthConfig(
            enabled=True,
            keys=[_key(VALID_KEY), _key(SECOND_KEY)],
        )
        app = _make_app(config)
        return TestClient(app)

    # -- Valid key ----------------------------------------------------------

    def test_valid_key_allows_access(self, client):
        response = client.get('/api/v1/notes', headers={'X-API-Key': VALID_KEY})
        assert response.status_code == 200

    def test_second_valid_key_allows_access(self, client):
        response = client.get('/api/v1/notes', headers={'X-API-Key': SECOND_KEY})
        assert response.status_code == 200

    def test_valid_key_post(self, client):
        response = client.post('/api/v1/ingestions/text', headers={'X-API-Key': VALID_KEY})
        assert response.status_code == 200

    # -- Missing key --------------------------------------------------------

    def test_missing_key_returns_401(self, client):
        response = client.get('/api/v1/notes')
        assert response.status_code == 401
        assert 'Missing API key' in response.json()['detail']

    # -- Invalid key --------------------------------------------------------

    def test_invalid_key_returns_403(self, client):
        response = client.get('/api/v1/notes', headers={'X-API-Key': 'wrong'})
        assert response.status_code == 403
        assert 'Invalid API key' in response.json()['detail']

    # -- Exempt paths -------------------------------------------------------

    def test_health_exempt(self, client):
        response = client.get('/api/v1/health')
        assert response.status_code == 200

    def test_ready_exempt(self, client):
        response = client.get('/api/v1/ready')
        assert response.status_code == 200

    def test_metrics_exempt(self, client):
        response = client.get('/api/v1/metrics')
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Custom exempt paths
# ---------------------------------------------------------------------------


class TestCustomExemptPaths:
    """Users can customize which paths are exempt from auth."""

    def test_custom_exempt_path_passes(self):
        config = AuthConfig(
            enabled=True,
            keys=[_key(VALID_KEY)],
            exempt_paths=['/api/v1/notes'],
        )
        app = _make_app(config)
        client = TestClient(app)
        # /notes is exempt, should pass without key
        response = client.get('/api/v1/notes')
        assert response.status_code == 200
        # /health is NOT exempt when overridden
        response = client.get('/api/v1/health')
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestAuthEdgeCases:
    """Edge cases for auth middleware."""

    def test_enabled_with_no_keys_rejects_all(self):
        """Auth enabled but no keys configured => all non-exempt requests rejected."""
        config = AuthConfig(enabled=True, keys=[])
        app = _make_app(config)
        client = TestClient(app)

        response = client.get('/api/v1/notes', headers={'X-API-Key': 'any'})
        assert response.status_code == 403

    def test_empty_api_key_header_returns_401(self):
        """An empty X-API-Key header should be treated as missing."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(config)
        client = TestClient(app)

        response = client.get('/api/v1/notes', headers={'X-API-Key': ''})
        # Empty string is falsy => 401
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# setup_auth stores config on app.state
# ---------------------------------------------------------------------------


class TestSetupAuth:
    """Tests for the setup_auth function itself."""

    def test_stores_config_on_state(self):
        app = FastAPI()
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        setup_auth(app, config)
        assert app.state.auth_config is config

    def test_disabled_no_middleware(self):
        """When disabled, no middleware should be added and no auth_config stored."""
        app = FastAPI()
        setup_auth(app, AuthConfig(enabled=False))
        assert not hasattr(app.state, 'auth_config')


# ---------------------------------------------------------------------------
# CVE-2026-48710 BadHost regression tests
# ---------------------------------------------------------------------------


class TestBadHostCVE202648710:
    """Regression tests for CVE-2026-48710 (BadHost) — host-header auth bypass.

    Starlette < 1.0.1 reconstructs ``request.url`` by concatenating the
    client-supplied ``Host`` header with the request path and re-parsing the
    result. A ``Host`` value like ``localhost/api/v1/health?`` makes
    ``request.url.path`` collapse onto ``'/api/v1/health'`` (an exempt path)
    while ``scope['path']`` — what the router actually dispatches on — stays
    ``'/api/v1/notes'``. Auth middleware that compares ``request.url.path``
    against ``exempt_paths`` is therefore bypassed.

    The fix uses ``request.scope['path']`` for security decisions. ``scope['path']``
    comes from the ASGI server directly, matches what the router uses, and is
    immune regardless of installed Starlette version.

    These tests assert the property directly. They simulate the Starlette bug
    by hijacking ``Request.url`` so it returns a different path than
    ``scope['path']``; a middleware that uses ``request.url.path`` will
    bypass auth, a middleware that uses ``scope['path']`` will enforce it.
    """

    @staticmethod
    def _patch_url_to_exempt_path(monkeypatch, exempt_path: str = '/api/v1/health') -> None:
        """Make ``Request.url.path`` always return *exempt_path* regardless of scope."""
        from starlette.datastructures import URL
        from starlette.requests import Request as StarletteRequest

        def hijacked_url(self):  # type: ignore[no-untyped-def]
            return URL(f'http://attacker.example{exempt_path}')

        monkeypatch.setattr(StarletteRequest, 'url', property(hijacked_url))

    def test_badhost_path_divergence_does_not_bypass_auth(self, monkeypatch):
        """Hijacked request.url.path pointing at an exempt route must NOT bypass auth."""
        self._patch_url_to_exempt_path(monkeypatch, '/api/v1/health')

        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(config)
        client = TestClient(app)

        # /api/v1/notes is protected. Even though url.path now claims to be
        # '/api/v1/health' (an exempt path), the dispatcher uses scope['path']
        # and runs the /notes handler. The middleware must do the same and
        # require a key.
        response = client.get('/api/v1/notes')
        assert response.status_code == 401, (
            f'CVE-2026-48710 regression: got {response.status_code}; protected '
            "'/api/v1/notes' was reached without an API key because the middleware "
            "trusted request.url.path (spoofed) instead of scope['path']."
        )

    def test_badhost_path_divergence_with_invalid_key_returns_403(self, monkeypatch):
        """Same attack with a wrong key still hits the validate step → 403, not 200."""
        self._patch_url_to_exempt_path(monkeypatch, '/api/v1/health')

        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(config)
        client = TestClient(app)

        response = client.get('/api/v1/notes', headers={'X-API-Key': 'wrong'})
        assert response.status_code == 403

    def test_clean_host_still_serves_exempt_paths(self):
        """Regression: legitimate exempt-path requests still work without a key."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(config)
        client = TestClient(app)
        response = client.get('/api/v1/health')
        assert response.status_code == 200

    def test_clean_host_still_requires_key_on_protected_routes(self):
        """Regression: protected routes still 401 without a key on clean Host."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(config)
        client = TestClient(app)
        response = client.get('/api/v1/notes')
        assert response.status_code == 401


class TestBadHostAdminAuditPath:
    """CVE-2026-48710 (BadHost): ``require_admin_auth`` records ``scope['path']``.

    ``require_admin_auth`` has no exempt-path branch, so the BadHost spoof
    cannot bypass it — but it writes the request path into five admin audit
    events. A spoofed ``request.url.path`` would poison that audit trail. This
    pins the audit detail to the unspoofable dispatched path.
    """

    def test_admin_missing_key_audits_scope_path_not_spoofed_url(self, monkeypatch):
        from starlette.datastructures import URL
        from starlette.requests import Request as StarletteRequest

        # Simulate the BadHost reconstruction: request.url.path claims an exempt path.
        monkeypatch.setattr(
            StarletteRequest,
            'url',
            property(lambda self: URL('http://attacker.example/api/v1/health')),
        )

        # Auth disabled globally → the global middleware passes through, so the
        # route-level require_admin_auth dependency is what enforces and audits.
        app = _make_app(AuthConfig(enabled=False))

        captured: list[dict] = []

        class _CapturingAudit:
            def log(self, **kwargs):
                captured.append(kwargs)

        app.state.audit_service = _CapturingAudit()

        @app.get('/api/v1/admin/thing', dependencies=[Depends(require_admin_auth)])
        async def _admin_thing():
            return {'ok': True}

        client = TestClient(app)
        response = client.get('/api/v1/admin/thing')  # no X-API-Key

        assert response.status_code == 401
        events = [e for e in captured if e.get('action') == 'auth.admin.missing_key']
        assert events, 'expected an auth.admin.missing_key audit event'
        assert events[0]['details']['path'] == '/api/v1/admin/thing', (
            'CVE-2026-48710 regression: require_admin_auth wrote the spoofed '
            "request.url.path into the audit detail instead of scope['path']."
        )


# ---------------------------------------------------------------------------
# Policy permissions mapping
# ---------------------------------------------------------------------------


class TestPolicyPermissions:
    """Tests for the POLICY_PERMISSIONS mapping."""

    def test_reader_has_read_only(self):
        assert POLICY_PERMISSIONS[Policy.READER] == frozenset({Permission.READ})

    def test_writer_has_read_and_write(self):
        assert POLICY_PERMISSIONS[Policy.WRITER] == frozenset({Permission.READ, Permission.WRITE})

    def test_admin_has_all(self):
        assert POLICY_PERMISSIONS[Policy.ADMIN] == frozenset(
            {Permission.READ, Permission.WRITE, Permission.DELETE}
        )


# ---------------------------------------------------------------------------
# _resolve_key tests
# ---------------------------------------------------------------------------


class TestResolveKey:
    """Tests for the _resolve_key helper function."""

    def test_returns_matching_config(self):
        config = AuthConfig(keys=[_key(VALID_KEY, Policy.WRITER)])
        result = _resolve_key(VALID_KEY, config)
        assert result is not None
        assert result.policy == Policy.WRITER

    def test_returns_none_for_invalid(self):
        config = AuthConfig(keys=[_key(VALID_KEY)])
        assert _resolve_key('wrong', config) is None

    def test_returns_first_match(self):
        config = AuthConfig(keys=[_key(VALID_KEY, Policy.READER), _key(SECOND_KEY, Policy.ADMIN)])
        result = _resolve_key(SECOND_KEY, config)
        assert result is not None
        assert result.policy == Policy.ADMIN


# ---------------------------------------------------------------------------
# AuthContext in middleware
# ---------------------------------------------------------------------------


class TestAuthContext:
    """Tests that the middleware attaches AuthContext to request state."""

    def test_auth_context_set_on_valid_key(self):
        """A valid key should result in AuthContext being set on request.state."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.WRITER)])
        app = _make_app(config)

        captured_context: dict = {}

        @app.get('/api/v1/check-context')
        async def check_context(request: Request):
            captured_context['auth'] = getattr(request.state, 'auth_context', None)
            return {'ok': True}

        client = TestClient(app)
        response = client.get('/api/v1/check-context', headers={'X-API-Key': VALID_KEY})
        assert response.status_code == 200
        auth = captured_context['auth']
        assert auth is not None
        assert auth.policy == Policy.WRITER
        assert auth.permissions == frozenset({Permission.READ, Permission.WRITE})
        assert auth.vault_ids is None

    def test_auth_context_not_set_when_disabled(self):
        """When auth is disabled, auth_context should not be set."""
        config = AuthConfig(enabled=False)
        app = _make_app(config)

        captured_context: dict = {}

        @app.get('/api/v1/check-context')
        async def check_context(request: Request):
            captured_context['auth'] = getattr(request.state, 'auth_context', None)
            return {'ok': True}

        client = TestClient(app)
        response = client.get('/api/v1/check-context')
        assert response.status_code == 200
        assert captured_context['auth'] is None

    def test_auth_context_has_vault_ids(self):
        """Vault-scoped keys should have vault_ids in AuthContext."""
        key_config = ApiKeyConfig(
            key=SecretStr(VALID_KEY),
            policy=Policy.READER,
            vault_ids=['vault-a', 'vault-b'],
        )
        config = AuthConfig(enabled=True, keys=[key_config])
        app = _make_app(config)

        captured_context: dict = {}

        @app.get('/api/v1/check-context')
        async def check_context(request: Request):
            captured_context['auth'] = getattr(request.state, 'auth_context', None)
            return {'ok': True}

        client = TestClient(app)
        response = client.get('/api/v1/check-context', headers={'X-API-Key': VALID_KEY})
        assert response.status_code == 200
        auth = captured_context['auth']
        assert auth.vault_ids == ['vault-a', 'vault-b']

    def test_auth_context_has_read_vault_ids(self):
        """Keys with read_vault_ids should propagate them to AuthContext."""
        key_config = ApiKeyConfig(
            key=SecretStr(VALID_KEY),
            policy=Policy.WRITER,
            vault_ids=['vault-a'],
            read_vault_ids=['vault-b', 'vault-c'],
        )
        config = AuthConfig(enabled=True, keys=[key_config])
        app = _make_app(config)

        captured_context: dict = {}

        @app.get('/api/v1/check-context')
        async def check_context(request: Request):
            captured_context['auth'] = getattr(request.state, 'auth_context', None)
            return {'ok': True}

        client = TestClient(app)
        response = client.get('/api/v1/check-context', headers={'X-API-Key': VALID_KEY})
        assert response.status_code == 200
        auth = captured_context['auth']
        assert auth.vault_ids == ['vault-a']
        assert auth.read_vault_ids == ['vault-b', 'vault-c']

    def test_auth_context_read_vault_ids_none_by_default(self):
        """Keys without read_vault_ids should have None in AuthContext."""
        key_config = ApiKeyConfig(
            key=SecretStr(VALID_KEY),
            policy=Policy.WRITER,
            vault_ids=['vault-a'],
        )
        config = AuthConfig(enabled=True, keys=[key_config])
        app = _make_app(config)

        captured_context: dict = {}

        @app.get('/api/v1/check-context')
        async def check_context(request: Request):
            captured_context['auth'] = getattr(request.state, 'auth_context', None)
            return {'ok': True}

        client = TestClient(app)
        response = client.get('/api/v1/check-context', headers={'X-API-Key': VALID_KEY})
        assert response.status_code == 200
        auth = captured_context['auth']
        assert auth.read_vault_ids is None


# ---------------------------------------------------------------------------
# Permission enforcement
# ---------------------------------------------------------------------------


class TestPermissionEnforcement:
    """Tests for require_read, require_write, require_delete dependencies."""

    @staticmethod
    def _make_permission_app(auth_config: AuthConfig) -> FastAPI:
        """Create a FastAPI app with permission-gated endpoints."""
        from fastapi import Depends

        app = FastAPI()
        app.middleware('http')(auth_middleware)
        setup_auth(app, auth_config)

        @app.get('/read', dependencies=[Depends(require_read)])
        async def read_endpoint():
            return {'ok': True}

        @app.post('/write', dependencies=[Depends(require_write)])
        async def write_endpoint():
            return {'ok': True}

        @app.delete('/delete', dependencies=[Depends(require_delete)])
        async def delete_endpoint():
            return {'ok': True}

        return app

    def test_reader_can_read(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.READER)])
        client = TestClient(self._make_permission_app(config))
        assert client.get('/read', headers={'X-API-Key': VALID_KEY}).status_code == 200

    def test_reader_cannot_write(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.READER)])
        client = TestClient(self._make_permission_app(config))
        assert client.post('/write', headers={'X-API-Key': VALID_KEY}).status_code == 403

    def test_reader_cannot_delete(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.READER)])
        client = TestClient(self._make_permission_app(config))
        assert client.delete('/delete', headers={'X-API-Key': VALID_KEY}).status_code == 403

    def test_writer_can_read(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.WRITER)])
        client = TestClient(self._make_permission_app(config))
        assert client.get('/read', headers={'X-API-Key': VALID_KEY}).status_code == 200

    def test_writer_can_write(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.WRITER)])
        client = TestClient(self._make_permission_app(config))
        assert client.post('/write', headers={'X-API-Key': VALID_KEY}).status_code == 200

    def test_writer_cannot_delete(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.WRITER)])
        client = TestClient(self._make_permission_app(config))
        assert client.delete('/delete', headers={'X-API-Key': VALID_KEY}).status_code == 403

    def test_admin_can_all(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.ADMIN)])
        client = TestClient(self._make_permission_app(config))
        assert client.get('/read', headers={'X-API-Key': VALID_KEY}).status_code == 200
        assert client.post('/write', headers={'X-API-Key': VALID_KEY}).status_code == 200
        assert client.delete('/delete', headers={'X-API-Key': VALID_KEY}).status_code == 200

    def test_auth_disabled_passes_all(self):
        """When auth is disabled, permission dependencies should pass through."""
        config = AuthConfig(enabled=False)
        client = TestClient(self._make_permission_app(config))
        assert client.get('/read').status_code == 200
        assert client.post('/write').status_code == 200
        assert client.delete('/delete').status_code == 200

    def test_403_includes_required_permission(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.READER)])
        client = TestClient(self._make_permission_app(config))
        resp = client.post('/write', headers={'X-API-Key': VALID_KEY})
        assert resp.status_code == 403
        assert 'write' in resp.json()['detail'].lower()


# ---------------------------------------------------------------------------
# Admin auth with policies
# ---------------------------------------------------------------------------


class TestAdminAuthWithPolicies:
    """Tests for require_admin_auth with the new policy system."""

    @staticmethod
    def _make_admin_app(auth_config: AuthConfig) -> FastAPI:
        from fastapi import Depends

        require_admin_auth = _auth_mod.require_admin_auth

        app = FastAPI()
        app.middleware('http')(auth_middleware)
        setup_auth(app, auth_config)

        @app.get('/admin/test', dependencies=[Depends(require_admin_auth)])
        async def admin_endpoint():
            return {'ok': True}

        return app

    def test_admin_key_allowed(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.ADMIN)])
        client = TestClient(self._make_admin_app(config))
        assert client.get('/admin/test', headers={'X-API-Key': VALID_KEY}).status_code == 200

    def test_writer_key_rejected(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.WRITER)])
        client = TestClient(self._make_admin_app(config))
        resp = client.get('/admin/test', headers={'X-API-Key': VALID_KEY})
        assert resp.status_code == 403

    def test_reader_key_rejected(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.READER)])
        client = TestClient(self._make_admin_app(config))
        resp = client.get('/admin/test', headers={'X-API-Key': VALID_KEY})
        assert resp.status_code == 403

    def test_no_key_returns_401(self):
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY, Policy.ADMIN)])
        client = TestClient(self._make_admin_app(config))
        assert client.get('/admin/test').status_code == 401

    def test_auth_disabled_blocks_admin(self):
        """Admin endpoints should be blocked when auth is disabled (fail-closed)."""
        config = AuthConfig(enabled=False)
        client = TestClient(self._make_admin_app(config))
        # No auth config stored on app.state → fail-closed
        assert client.get('/admin/test', headers={'X-API-Key': 'any'}).status_code == 403


# ---------------------------------------------------------------------------
# OPTIONS method exemption (CORS preflight)
# ---------------------------------------------------------------------------


class TestOptionsExemption:
    """OPTIONS requests must pass through auth for CORS preflight to work."""

    def test_options_passes_without_key(self):
        """OPTIONS to a protected path should pass without an API key."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(config)
        client = TestClient(app)
        response = client.options('/api/v1/notes')
        # Should not be 401/403 — auth middleware lets it through
        assert response.status_code != 401
        assert response.status_code != 403

    def test_options_passes_when_auth_disabled(self):
        """OPTIONS should also work when auth is disabled."""
        app = _make_app(AuthConfig(enabled=False))
        client = TestClient(app)
        response = client.options('/api/v1/notes')
        assert response.status_code != 401
        assert response.status_code != 403

    def test_get_still_requires_key(self):
        """GET to the same protected path still requires auth."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = _make_app(config)
        client = TestClient(app)
        assert client.get('/api/v1/notes').status_code == 401


# ---------------------------------------------------------------------------
# CORS integration (CORSMiddleware + auth_middleware)
# ---------------------------------------------------------------------------


class TestCorsIntegration:
    """Test that CORSMiddleware and auth_middleware work together correctly."""

    @staticmethod
    def _make_cors_app(
        auth_config: AuthConfig, origins: list[str] | None = None, origin_regex: str | None = None
    ) -> FastAPI:
        """Create a FastAPI app with both CORS and auth middleware."""
        from fastapi.middleware.cors import CORSMiddleware

        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins or ['http://localhost:3000'],
            allow_origin_regex=origin_regex,
            allow_credentials=True,
            allow_methods=['*'],
            allow_headers=['*'],
        )
        app.middleware('http')(auth_middleware)
        setup_auth(app, auth_config)

        @app.get('/api/v1/notes')
        async def notes():
            return {'notes': []}

        return app

    def test_preflight_from_allowed_origin(self):
        """Preflight from an allowed origin should get CORS headers."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = self._make_cors_app(config, origins=['http://localhost:3000'])
        client = TestClient(app)
        response = client.options(
            '/api/v1/notes',
            headers={
                'Origin': 'http://localhost:3000',
                'Access-Control-Request-Method': 'GET',
                'Access-Control-Request-Headers': 'X-API-Key',
            },
        )
        assert response.status_code == 200
        assert 'access-control-allow-origin' in response.headers

    def test_preflight_from_extension_origin(self):
        """Preflight from a browser extension origin should work with regex."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = self._make_cors_app(
            config,
            origins=[],
            origin_regex=r'(moz|chrome)-extension://.*',
        )
        client = TestClient(app)
        response = client.options(
            '/api/v1/notes',
            headers={
                'Origin': 'moz-extension://abc-123-def',
                'Access-Control-Request-Method': 'GET',
                'Access-Control-Request-Headers': 'X-API-Key',
            },
        )
        assert response.status_code == 200
        assert response.headers.get('access-control-allow-origin') == 'moz-extension://abc-123-def'

    def test_preflight_from_disallowed_origin(self):
        """Preflight from an unrecognised origin should be rejected by CORS."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = self._make_cors_app(config, origins=['http://localhost:3000'])
        client = TestClient(app)
        response = client.options(
            '/api/v1/notes',
            headers={
                'Origin': 'https://evil.com',
                'Access-Control-Request-Method': 'GET',
                'Access-Control-Request-Headers': 'X-API-Key',
            },
        )
        assert response.status_code == 400

    def test_actual_request_from_extension_with_key(self):
        """GET from extension origin with valid API key should succeed."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = self._make_cors_app(
            config,
            origins=[],
            origin_regex=r'(moz|chrome)-extension://.*',
        )
        client = TestClient(app)
        response = client.get(
            '/api/v1/notes',
            headers={
                'Origin': 'moz-extension://abc-123-def',
                'X-API-Key': VALID_KEY,
            },
        )
        assert response.status_code == 200
        assert response.headers.get('access-control-allow-origin') == 'moz-extension://abc-123-def'

    def test_preflight_from_null_origin(self):
        """Preflight from null origin (sandboxed iframe in about:addons) should work."""
        config = AuthConfig(enabled=True, keys=[_key(VALID_KEY)])
        app = self._make_cors_app(config, origins=['null'])
        client = TestClient(app)
        response = client.options(
            '/api/v1/notes',
            headers={
                'Origin': 'null',
                'Access-Control-Request-Method': 'GET',
                'Access-Control-Request-Headers': 'X-API-Key',
            },
        )
        assert response.status_code == 200
        assert response.headers.get('access-control-allow-origin') == 'null'
