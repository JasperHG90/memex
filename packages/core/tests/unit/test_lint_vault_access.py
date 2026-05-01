"""Per-vault auth scoping for the F8 lint router (CRITICAL-001).

The three vault-scoped read endpoints (`/lint/status`, `/lint/findings`,
`/lint/flags`) MUST reject callers whose AuthContext does not include the
requested vault. Without these tests a reader scoped to vault-A could probe
vault-B's lint dashboard via the F8 ``memex_get_lint_flags`` surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from memex_common.config import Permission, Policy, POLICY_PERMISSIONS
from memex_core.server import app
from memex_core.server.auth import AuthContext, get_auth_context
from memex_core.server.common import get_api


ALLOWED_VAULT = uuid4()
FORBIDDEN_VAULT = uuid4()


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.config = SimpleNamespace(server=SimpleNamespace(default_active_vault='vault-a'))

    async def _resolve(identifier):
        if isinstance(identifier, UUID):
            return identifier
        try:
            return UUID(str(identifier))
        except (ValueError, AttributeError):
            raise ValueError(f'Unknown vault: {identifier}')

    api.resolve_vault_identifier = AsyncMock(side_effect=_resolve)
    api.lint = AsyncMock()
    api.lint.count_pending = AsyncMock(return_value=0)
    api.lint.get_findings = AsyncMock(return_value=SimpleNamespace(findings=[], next_cursor=None))
    api.metastore = SimpleNamespace()
    return api


def _make_client(mock_api, auth: AuthContext | None) -> TestClient:
    app.dependency_overrides[get_api] = lambda: mock_api
    app.dependency_overrides[get_auth_context] = lambda: auth
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides = {}


def _scoped_reader() -> AuthContext:
    return AuthContext(
        key_prefix='test1234',
        key_name='scoped-reader',
        policy=Policy.READER,
        permissions=POLICY_PERMISSIONS[Policy.READER],
        vault_ids=[str(ALLOWED_VAULT)],
        read_vault_ids=None,
    )


def _unrestricted_reader() -> AuthContext:
    return AuthContext(
        key_prefix='test1234',
        key_name='admin',
        policy=Policy.READER,
        permissions=POLICY_PERMISSIONS[Policy.READER],
        vault_ids=None,
        read_vault_ids=None,
    )


# ---------------------------------------------------------------------------
# Deny path
# ---------------------------------------------------------------------------


class TestLintForbiddenVault:
    def test_status_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/lint/status?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 403, resp.text
        mock_api.lint.count_pending.assert_not_called()

    def test_findings_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/lint/findings?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 403, resp.text

    def test_flags_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/lint/flags?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 403, resp.text
        mock_api.lint.get_findings.assert_not_called()


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


class TestLintAllowedVault:
    def test_status_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/lint/status?vault_id={ALLOWED_VAULT}')
        assert resp.status_code == 200, resp.text
        mock_api.lint.count_pending.assert_awaited_once()

    def test_flags_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/lint/flags?vault_id={ALLOWED_VAULT}')
        assert resp.status_code == 200, resp.text
        mock_api.lint.get_findings.assert_awaited_once()

    def test_unrestricted_key_allows_any_vault(self, mock_api):
        client = _make_client(mock_api, _unrestricted_reader())
        resp = client.get(f'/api/v1/lint/flags?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# No-auth (auth disabled)
# ---------------------------------------------------------------------------


class TestLintNoAuth:
    def test_flags_with_no_auth_passes_through(self, mock_api):
        client = _make_client(mock_api, auth=None)
        resp = client.get(f'/api/v1/lint/flags?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Read-extra: a read_vault_ids entry must also unlock the lint flag reads.
# ---------------------------------------------------------------------------


class TestLintReadExtras:
    def test_read_vault_ids_grants_lint_flags(self, mock_api):
        auth = AuthContext(
            key_prefix='test1234',
            key_name='scoped-writer',
            policy=Policy.WRITER,
            permissions=frozenset({Permission.READ, Permission.WRITE}),
            vault_ids=[str(ALLOWED_VAULT)],
            read_vault_ids=[str(FORBIDDEN_VAULT)],
        )
        client = _make_client(mock_api, auth)
        resp = client.get(f'/api/v1/lint/flags?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text
