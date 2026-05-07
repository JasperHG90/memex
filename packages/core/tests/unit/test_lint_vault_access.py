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
    # Default: finding belongs to ALLOWED_VAULT (tests override per-case).
    api.lint.get_finding_vault_id = AsyncMock(return_value=(True, ALLOWED_VAULT))
    api.lint.set_status = AsyncMock(return_value=True)
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
# /lint/status scope contract: scope=global must NOT count per-vault, and
# explicit scope+vault_id combinations must be rejected with 400.
# ---------------------------------------------------------------------------


class TestLintStatusScopeContract:
    def test_scope_global_returns_global_count(self, mock_api):
        # count_pending(None) returns the global count.
        mock_api.lint.count_pending = AsyncMock(return_value=7)
        client = _make_client(mock_api, _unrestricted_reader())
        resp = client.get('/api/v1/lint/status?scope=global')
        assert resp.status_code == 200, resp.text
        assert resp.json() == {'scope': 'global', 'pending': 7}
        mock_api.lint.count_pending.assert_awaited_once_with(None)

    def test_scope_global_rejects_vault_id(self, mock_api):
        client = _make_client(mock_api, _unrestricted_reader())
        resp = client.get(f'/api/v1/lint/status?scope=global&vault_id={ALLOWED_VAULT}')
        assert resp.status_code == 400, resp.text
        mock_api.lint.count_pending.assert_not_called()

    def test_scope_all_rejects_vault_id(self, mock_api):
        client = _make_client(mock_api, _unrestricted_reader())
        resp = client.get(f'/api/v1/lint/status?scope=all&vault_id={ALLOWED_VAULT}')
        assert resp.status_code == 400, resp.text

    def test_scope_vault_without_vault_id_returns_400(self, mock_api):
        client = _make_client(mock_api, _unrestricted_reader())
        resp = client.get('/api/v1/lint/status?scope=vault')
        assert resp.status_code == 400, resp.text

    def test_implicit_scope_vault_when_only_vault_id_supplied(self, mock_api):
        # No scope param → infer scope=vault from vault_id, run auth check,
        # call count_pending(vault_id), and return per-vault payload.
        mock_api.lint.count_pending = AsyncMock(return_value=3)
        client = _make_client(mock_api, _unrestricted_reader())
        resp = client.get(f'/api/v1/lint/status?vault_id={ALLOWED_VAULT}')
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body['scope'] == 'vault'
        assert body['vault_id'] == str(ALLOWED_VAULT)
        assert body['pending'] == 3


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


# ---------------------------------------------------------------------------
# F8 mutation routes — /findings/{id}/dismiss + /resolve (HIGH-4 sub)
#
# These verbs operate on a bare finding_id and previously had no per-vault
# auth check; the route now resolves the finding's vault_id and gates against
# the auth context. ``LintService.set_status`` also takes the vault_id and
# constrains the UPDATE WHERE — defense-in-depth.
# ---------------------------------------------------------------------------


def _scoped_writer() -> AuthContext:
    return AuthContext(
        key_prefix='test1234',
        key_name='scoped-writer',
        policy=Policy.WRITER,
        permissions=POLICY_PERMISSIONS[Policy.WRITER],
        vault_ids=[str(ALLOWED_VAULT)],
        read_vault_ids=None,
    )


FINDING_ID = uuid4()


class TestLintMutationForbiddenVault:
    def test_dismiss_blocks_when_finding_belongs_to_other_vault(self, mock_api):
        # Finding belongs to FORBIDDEN_VAULT; caller is scoped to ALLOWED_VAULT.
        mock_api.lint.get_finding_vault_id = AsyncMock(return_value=(True, FORBIDDEN_VAULT))
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/dismiss')
        assert resp.status_code == 403, resp.text
        mock_api.lint.set_status.assert_not_called()

    def test_resolve_blocks_when_finding_belongs_to_other_vault(self, mock_api):
        mock_api.lint.get_finding_vault_id = AsyncMock(return_value=(True, FORBIDDEN_VAULT))
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/resolve')
        assert resp.status_code == 403, resp.text
        mock_api.lint.set_status.assert_not_called()

    def test_dismiss_returns_404_when_finding_not_found(self, mock_api):
        mock_api.lint.get_finding_vault_id = AsyncMock(return_value=(False, None))
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/dismiss')
        assert resp.status_code == 404, resp.text
        mock_api.lint.set_status.assert_not_called()


class TestLintMutationAllowedVault:
    def test_dismiss_allows_in_scope_finding(self, mock_api):
        mock_api.lint.get_finding_vault_id = AsyncMock(return_value=(True, ALLOWED_VAULT))
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/dismiss')
        assert resp.status_code == 200, resp.text
        # Service receives the finding's vault_id for SQL-level filter.
        mock_api.lint.set_status.assert_awaited_once()
        call = mock_api.lint.set_status.await_args
        assert call.kwargs['vault_id'] == ALLOWED_VAULT

    def test_resolve_allows_in_scope_finding(self, mock_api):
        mock_api.lint.get_finding_vault_id = AsyncMock(return_value=(True, ALLOWED_VAULT))
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/resolve')
        assert resp.status_code == 200, resp.text
        mock_api.lint.set_status.assert_awaited_once()


class TestLintMutationNoAuth:
    def test_dismiss_with_no_auth_passes_through(self, mock_api):
        mock_api.lint.get_finding_vault_id = AsyncMock(return_value=(True, FORBIDDEN_VAULT))
        client = _make_client(mock_api, auth=None)
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/dismiss')
        assert resp.status_code == 200, resp.text
        mock_api.lint.set_status.assert_awaited_once()


class TestLintMutationGlobalFinding:
    def test_global_finding_bypasses_per_vault_gate(self, mock_api):
        """A finding with vault_id NULL is global — the per-vault auth gate
        does not apply. The service-layer SQL filter still constrains by
        ``vault_id IS NULL`` (passed through as ``None``).
        """
        mock_api.lint.get_finding_vault_id = AsyncMock(return_value=(True, None))
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/dismiss')
        assert resp.status_code == 200, resp.text
        mock_api.lint.set_status.assert_awaited_once()
        call = mock_api.lint.set_status.await_args
        assert call.kwargs['vault_id'] is None
