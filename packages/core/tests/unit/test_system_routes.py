"""Unit tests for /api/v1/system/config endpoint.

Verifies:
- Returned shape mirrors ``MemexConfig.model_dump(mode='json')`` with
  secrets redacted via ``memex_common.redaction.redact``.
- The redaction walker actually masks every ``SecretStr`` field in the
  resolved config (defense-in-depth: even if Pydantic's default
  ``SecretStr`` handling regressed, the walker would still catch it).

Admin-auth gating is verified at the integration layer alongside the
existing ``test_e2e_auth_acl.py`` admin-only endpoint tests, since unit
tests bypass the auth middleware via mocked ``app.state``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from memex_common.config import MemexConfig
from memex_core.server import app
from memex_core.server.auth import require_admin_auth


@pytest.fixture
def client_with_real_config() -> TestClient:
    """TestClient with a real MemexConfig containing known secrets in app.state.

    Bypasses admin-auth via FastAPI dependency_overrides so this test can
    isolate the endpoint logic. Admin-auth enforcement is verified at the
    integration layer (testcontainer-backed), not here.
    """
    sentinel = 'TESTING_SENTINEL_v3'
    cfg = MemexConfig.model_validate(
        {
            'server': {
                'meta_store': {
                    'type': 'postgres',
                    'instance': {
                        'host': 'localhost',
                        'port': 5432,
                        'database': 'memex',
                        'user': 'memex',
                        'password': sentinel,
                    },
                },
                'auth': {
                    'enabled': True,
                    'keys': [{'key': sentinel, 'policy': 'admin'}],
                    'webhook_secret': sentinel,
                },
                'file_store': {
                    'type': 's3',
                    'bucket': 'memex-test',
                    'access_key_id': sentinel,
                    'secret_access_key': sentinel,
                    'session_token': sentinel,
                },
            },
        }
    )

    mock_api = SimpleNamespace(config=cfg)
    app.state.api = mock_api
    # Bypass admin-auth for this unit test (gated separately at integration layer)
    app.dependency_overrides[require_admin_auth] = lambda: None

    yield TestClient(app)

    if hasattr(app.state, 'api'):
        del app.state.api
    app.dependency_overrides.pop(require_admin_auth, None)


def _walk_for_value(payload: object, target: str) -> bool:
    if isinstance(payload, dict):
        return any(_walk_for_value(v, target) for v in payload.values())
    if isinstance(payload, list):
        return any(_walk_for_value(v, target) for v in payload)
    if isinstance(payload, str):
        return target in payload
    return False


class TestSystemConfigEndpoint:
    """Tests for GET /api/v1/system/config."""

    def test_returns_200_when_admin_auth_bypassed(
        self, client_with_real_config: TestClient
    ) -> None:
        """With dependency_overrides[require_admin_auth] = lambda: None
        in place, the endpoint serves the redacted config."""
        resp = client_with_real_config.get('/api/v1/system/config')
        assert resp.status_code == 200, resp.text

    def test_returned_shape_is_dict(self, client_with_real_config: TestClient) -> None:
        resp = client_with_real_config.get('/api/v1/system/config')
        assert isinstance(resp.json(), dict)

    def test_no_secret_sentinel_leaks(self, client_with_real_config: TestClient) -> None:
        """The configured TESTING_SENTINEL must not appear anywhere in the response."""
        resp = client_with_real_config.get('/api/v1/system/config')
        body = resp.json()
        assert not _walk_for_value(body, 'TESTING_SENTINEL_v3'), (
            'system/config leaked a secret sentinel — redaction walker is broken'
        )

    def test_redacted_marker_present(self, client_with_real_config: TestClient) -> None:
        """The response should contain the <redacted> marker — otherwise either
        the test config has no secrets (vacuous test) or redaction is no-op."""
        resp = client_with_real_config.get('/api/v1/system/config')
        body = resp.json()
        assert _walk_for_value(body, '<redacted>'), (
            'system/config produced no <redacted> markers — test config or redactor is broken'
        )

    def test_non_secret_config_passes_through(self, client_with_real_config: TestClient) -> None:
        """Non-secret values like host/port/db should appear unmasked."""
        resp = client_with_real_config.get('/api/v1/system/config')
        body = resp.json()
        host = body['server']['meta_store']['instance']['host']
        assert host == 'localhost'
        port = body['server']['meta_store']['instance']['port']
        assert port == 5432

    def test_apikeyconfig_keys_redacted_in_list(self, client_with_real_config: TestClient) -> None:
        """ApiKeyConfig.key (in a list at AuthConfig.keys) must be redacted via the
        shape rule — substring match alone wouldn't catch plain `key`."""
        resp = client_with_real_config.get('/api/v1/system/config')
        body = resp.json()
        keys = body['server']['auth']['keys']
        assert len(keys) == 1
        assert keys[0]['key'] == '<redacted>'
        assert keys[0]['key_set'] is True
        assert keys[0]['policy'] == 'admin'

    def test_endpoint_is_get_only(self, client_with_real_config: TestClient) -> None:
        resp = client_with_real_config.post('/api/v1/system/config')
        assert resp.status_code == 405


class TestSystemConfigAdminAuthGate:
    """Verifies require_admin_auth actually rejects non-admin requests.

    Unlike TestSystemConfigEndpoint above (which bypasses auth via
    dependency_overrides), these tests exercise the real admin-auth
    dependency. We don't need a Postgres testcontainer for this — the
    admin-auth dependency only reads ``app.state.auth_config`` and
    request headers.
    """

    @pytest.fixture
    def client_with_admin_auth(self) -> TestClient:
        """TestClient with auth_config configured but no dependency override."""
        from memex_common.config import AuthConfig

        cfg = MemexConfig.model_validate({})
        mock_api = SimpleNamespace(config=cfg)
        app.state.api = mock_api
        # Configure auth (admin-only key) directly on app.state
        # bypassing the lifespan setup.
        app.state.auth_config = AuthConfig.model_validate(
            {
                'enabled': True,
                'keys': [
                    {'key': 'admin-key-secret', 'policy': 'admin'},
                    {'key': 'reader-key-secret', 'policy': 'reader'},
                ],
            }
        )
        yield TestClient(app)
        if hasattr(app.state, 'api'):
            del app.state.api
        if hasattr(app.state, 'auth_config'):
            del app.state.auth_config

    def test_no_api_key_returns_401(self, client_with_admin_auth: TestClient) -> None:
        resp = client_with_admin_auth.get('/api/v1/system/config')
        assert resp.status_code == 401

    def test_reader_key_returns_403(self, client_with_admin_auth: TestClient) -> None:
        resp = client_with_admin_auth.get(
            '/api/v1/system/config',
            headers={'X-API-Key': 'reader-key-secret'},
        )
        assert resp.status_code == 403

    def test_admin_key_returns_200(self, client_with_admin_auth: TestClient) -> None:
        resp = client_with_admin_auth.get(
            '/api/v1/system/config',
            headers={'X-API-Key': 'admin-key-secret'},
        )
        assert resp.status_code == 200, resp.text


class TestRedactionWalkerCoversFreshSecretField:
    """Defense-in-depth: even if the redaction walker regressed AND a future
    Pydantic upgrade exposed raw secrets, the walker's deny-list catches
    every secret-bearing leaf name."""

    def test_redaction_catches_every_secretstr_field_via_dump(self) -> None:
        from memex_common.redaction import redact

        # Build a synthetic config-shaped dict with raw strings (NOT SecretStr)
        # at every path where a SecretStr lives in MemexConfig. This simulates
        # a hypothetical Pydantic regression that exposes raw secret values.
        raw = {
            'server': {
                'instance': {'database': {'password': 'leak1'}},
                'auth': {
                    'keys': [{'key': 'leak2', 'policy': 'admin'}],
                    'webhook_secret': 'leak3',
                },
                'filestore': {
                    'access_key_id': 'AKIA-public',
                    'secret_access_key': 'leak4',
                    'session_token': 'leak5',
                },
                'memory': {
                    'extraction': {'model': {'api_key': 'leak6'}},
                    'embedding': {'api_key': 'leak7'},
                    'reranker': {'api_key': 'leak8'},
                    'nli': {'api_key': 'leak9'},
                },
            },
            'api_key': 'leak10',
        }
        out = redact(raw)
        for sentinel in (f'leak{i}' for i in range(1, 11)):
            assert not _walk_for_value(out, sentinel), (
                f'{sentinel} leaked through redact — deny-list is incomplete'
            )

    def test_pydantic_secretstr_baseline_still_holds(self) -> None:
        """Sanity baseline — if this fails, Pydantic changed SecretStr JSON
        serialization and we may need to revisit redaction strategy."""
        from pydantic import BaseModel

        class _M(BaseModel):
            api_key: SecretStr

        m = _M(api_key='real-secret-xyz')
        dumped = m.model_dump(mode='json')
        assert dumped == {'api_key': '**********'}
