"""Tests for API key authentication middleware."""

import importlib.util
import pathlib as plb

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from memex_common.config import AuthConfig

# Import auth module directly to avoid triggering server/__init__.py
# (which imports the full MemexAPI → retrieval chain).
import memex_core

_auth_path = plb.Path(memex_core.__file__).resolve().parent / 'server' / 'auth.py'
_spec = importlib.util.spec_from_file_location('_auth', _auth_path)
assert _spec is not None and _spec.loader is not None
_auth_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_auth_mod)
setup_auth = _auth_mod.setup_auth
_validate_key = _auth_mod._validate_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(auth_config: AuthConfig) -> FastAPI:
    """Create a minimal FastAPI app with auth middleware and a test endpoint."""
    app = FastAPI()
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


# ---------------------------------------------------------------------------
# AuthConfig tests
# ---------------------------------------------------------------------------


class TestAuthConfig:
    """Tests for the AuthConfig model defaults and validation."""

    def test_disabled_by_default(self):
        config = AuthConfig()
        assert config.enabled is False
        assert config.api_keys == []

    def test_default_exempt_paths(self):
        config = AuthConfig()
        assert '/api/v1/health' in config.exempt_paths
        assert '/api/v1/ready' in config.exempt_paths
        assert '/api/v1/metrics' in config.exempt_paths

    def test_custom_exempt_paths(self):
        config = AuthConfig(exempt_paths=['/custom'])
        assert config.exempt_paths == ['/custom']

    def test_api_keys_stored_as_secret(self):
        config = AuthConfig(api_keys=[SecretStr('my-key')])
        assert config.api_keys[0].get_secret_value() == 'my-key'


# ---------------------------------------------------------------------------
# _validate_key tests
# ---------------------------------------------------------------------------


class TestValidateKey:
    """Tests for the _validate_key helper function."""

    def test_valid_key_returns_true(self):
        config = AuthConfig(api_keys=[SecretStr(VALID_KEY)])
        assert _validate_key(VALID_KEY, config) is True

    def test_invalid_key_returns_false(self):
        config = AuthConfig(api_keys=[SecretStr(VALID_KEY)])
        assert _validate_key('wrong-key', config) is False

    def test_empty_keys_always_false(self):
        config = AuthConfig(api_keys=[])
        assert _validate_key('any-key', config) is False

    def test_multiple_keys_any_match(self):
        config = AuthConfig(api_keys=[SecretStr(VALID_KEY), SecretStr(SECOND_KEY)])
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
            api_keys=[SecretStr(VALID_KEY), SecretStr(SECOND_KEY)],
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
            api_keys=[SecretStr(VALID_KEY)],
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
        config = AuthConfig(enabled=True, api_keys=[])
        app = _make_app(config)
        client = TestClient(app)

        response = client.get('/api/v1/notes', headers={'X-API-Key': 'any'})
        assert response.status_code == 403

    def test_empty_api_key_header_returns_401(self):
        """An empty X-API-Key header should be treated as missing."""
        config = AuthConfig(enabled=True, api_keys=[SecretStr(VALID_KEY)])
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
        config = AuthConfig(enabled=True, api_keys=[SecretStr(VALID_KEY)])
        setup_auth(app, config)
        assert app.state.auth_config is config

    def test_disabled_no_middleware(self):
        """When disabled, no middleware should be added and no auth_config stored."""
        app = FastAPI()
        setup_auth(app, AuthConfig(enabled=False))
        assert not hasattr(app.state, 'auth_config')
