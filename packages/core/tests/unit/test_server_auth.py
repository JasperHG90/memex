"""Tests for API key authentication middleware."""

import importlib.util
import pathlib as plb

import pytest

from fastapi import FastAPI, Request
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
