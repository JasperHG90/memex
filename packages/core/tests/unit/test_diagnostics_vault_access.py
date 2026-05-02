"""Per-vault auth scoping for the F32 diagnostics router.

Each diagnostics endpoint that returns vault-scoped data MUST reject callers
whose AuthContext does not include the requested vault. Without these tests
a reader scoped to vault-A could probe vault-B's manifold/heatmap/summary/
lint dashboard via the diagnostics router (gap left by F32-core).

These are HTTP-level tests with the API mocked out — we verify the auth
gate fires *before* the underlying service is called (the AsyncMock has no
return value wired up, so a leak would either 500 or hand back the mock
sentinel).
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
    api.diagnostics = AsyncMock()
    api.diagnostics.get_or_compute_manifold = AsyncMock(return_value=('ready', {'projection': []}))
    api.diagnostics.get_manifold_status = AsyncMock(return_value=('ready', {'projection': []}))
    api.diagnostics.get_summary = AsyncMock(return_value={'vault_id': str(ALLOWED_VAULT)})
    api.diagnostics.get_lint_dashboard = AsyncMock(return_value={'pending_by_type': {}})
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
    """Reader that may only access ALLOWED_VAULT."""
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
# Deny path: scoped key targeting a forbidden vault → 403, service not called.
# ---------------------------------------------------------------------------


class TestDiagnosticsForbiddenVault:
    def test_manifold_status_endpoint_blocks_forbidden_vault(self, mock_api):
        """`/manifold/{vault_id}/status` does not need umap; cleanest deny path."""
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(
            f'/api/v1/diagnostics/manifold/{FORBIDDEN_VAULT}/status?task_id=t1',
        )
        assert resp.status_code == 403, resp.text
        mock_api.diagnostics.get_manifold_status.assert_not_called()

    def test_retrieval_heatmap_blocks_forbidden_vault(self, mock_api, monkeypatch):
        from memex_core.server import diagnostics as diag_module

        compute_spy = AsyncMock()
        monkeypatch.setattr(diag_module, 'compute_heatmap', compute_spy)

        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/diagnostics/retrieval/{FORBIDDEN_VAULT}')
        assert resp.status_code == 403, resp.text
        compute_spy.assert_not_called()

    def test_summary_endpoint_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/diagnostics/summary/{FORBIDDEN_VAULT}')
        assert resp.status_code == 403, resp.text
        mock_api.diagnostics.get_summary.assert_not_called()

    def test_lint_dashboard_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/diagnostics/lint/{FORBIDDEN_VAULT}')
        assert resp.status_code == 403, resp.text
        mock_api.diagnostics.get_lint_dashboard.assert_not_called()

    def test_manifold_endpoint_blocks_forbidden_vault(self, mock_api, monkeypatch):
        """`/manifold/{vault_id}` short-circuits on umap-missing — patch it
        so the auth gate is the only thing left to check."""
        from memex_core.server import diagnostics as diag_module

        monkeypatch.setattr(diag_module, '_ensure_umap_available', lambda: None)

        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/diagnostics/manifold/{FORBIDDEN_VAULT}')
        assert resp.status_code == 403, resp.text
        mock_api.diagnostics.get_or_compute_manifold.assert_not_called()


# ---------------------------------------------------------------------------
# Allow path: scoped key targeting its allowed vault → 200, service called.
# Confirms F32 happy path still works once the gate is in place.
# ---------------------------------------------------------------------------


class TestDiagnosticsAllowedVault:
    def test_summary_endpoint_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/diagnostics/summary/{ALLOWED_VAULT}')
        assert resp.status_code == 200, resp.text
        mock_api.diagnostics.get_summary.assert_awaited_once()

    def test_lint_dashboard_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/diagnostics/lint/{ALLOWED_VAULT}')
        assert resp.status_code == 200, resp.text
        mock_api.diagnostics.get_lint_dashboard.assert_awaited_once()

    def test_retrieval_heatmap_allows_in_scope_vault(self, mock_api, monkeypatch):
        from memex_core.server import diagnostics as diag_module

        compute_spy = AsyncMock(return_value={'vault_id': str(ALLOWED_VAULT), 'entities': []})
        monkeypatch.setattr(diag_module, 'compute_heatmap', compute_spy)

        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/diagnostics/retrieval/{ALLOWED_VAULT}')
        assert resp.status_code == 200, resp.text
        compute_spy.assert_awaited_once()

    def test_unrestricted_key_allows_any_vault(self, mock_api):
        """`vault_ids=None` (admin/unscoped key) bypasses the per-vault gate."""
        client = _make_client(mock_api, _unrestricted_reader())
        resp = client.get(f'/api/v1/diagnostics/summary/{FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# No-auth (auth disabled): every endpoint is reachable.
# Confirms we did not regress the no-auth deployment mode.
# ---------------------------------------------------------------------------


class TestDiagnosticsNoAuth:
    def test_summary_with_no_auth_passes_through(self, mock_api):
        client = _make_client(mock_api, auth=None)
        resp = client.get(f'/api/v1/diagnostics/summary/{FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Read-extra: a read_vault_ids entry must also unlock diagnostics.
# ---------------------------------------------------------------------------


class TestDiagnosticsReadExtras:
    def test_read_vault_ids_grants_read_diagnostics(self, mock_api):
        """A writer scoped to vault-A with read_vault_ids=[vault-B] can hit
        diagnostics for vault-B (READ permission)."""
        auth = AuthContext(
            key_prefix='test1234',
            key_name='scoped-writer',
            policy=Policy.WRITER,
            permissions=frozenset({Permission.READ, Permission.WRITE}),
            vault_ids=[str(ALLOWED_VAULT)],
            read_vault_ids=[str(FORBIDDEN_VAULT)],
        )
        client = _make_client(mock_api, auth)
        resp = client.get(f'/api/v1/diagnostics/summary/{FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text
