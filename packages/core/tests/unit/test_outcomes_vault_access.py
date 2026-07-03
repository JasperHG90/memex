"""Per-vault auth scoping for the F29 outcomes router (HIGH-4 sub).

The ``/api/v1/outcomes/record`` endpoint accepts a vault-scoped outcome
payload; without ``check_vault_access`` a key bound to vault-A could submit
an outcome that lands in vault-B's audit trail.

Mirrors the shape established by ``test_memories_vault_access.py``:
HTTP-level tests with the API mocked, asserting the auth gate fires *before*
``api.record_outcome`` is invoked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from memex_common.config import Policy, POLICY_PERMISSIONS
from memex_core.server import app
from memex_core.server.auth import AuthContext, get_auth_context
from memex_core.server.common import get_api


ALLOWED_VAULT = uuid4()
FORBIDDEN_VAULT = uuid4()
UNIT_ID = uuid4()


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
    api.record_outcome = AsyncMock(return_value={'recorded': True})
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


def _scoped_writer() -> AuthContext:
    return AuthContext(
        key_prefix='test1234',
        key_name='scoped-writer',
        policy=Policy.WRITER,
        permissions=POLICY_PERMISSIONS[Policy.WRITER],
        vault_ids=[str(ALLOWED_VAULT)],
        read_vault_ids=None,
    )


def _unrestricted_writer() -> AuthContext:
    return AuthContext(
        key_prefix='test1234',
        key_name='admin',
        policy=Policy.WRITER,
        permissions=POLICY_PERMISSIONS[Policy.WRITER],
        vault_ids=None,
        read_vault_ids=None,
    )


# ---------------------------------------------------------------------------
# Deny path
# ---------------------------------------------------------------------------


class TestOutcomesForbiddenVault:
    def test_record_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            '/api/v1/outcomes/record',
            json={
                'success': True,
                'unit_ids': [str(UNIT_ID)],
                'vault_id': str(FORBIDDEN_VAULT),
            },
        )
        assert resp.status_code == 403, resp.text
        mock_api.record_outcome.assert_not_called()


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


class TestOutcomesAllowedVault:
    def test_record_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            '/api/v1/outcomes/record',
            json={
                'success': True,
                'unit_ids': [str(UNIT_ID)],
                'vault_id': str(ALLOWED_VAULT),
            },
        )
        assert resp.status_code == 200, resp.text
        mock_api.record_outcome.assert_awaited_once()

    def test_unrestricted_writer_can_reach_any_vault(self, mock_api):
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/outcomes/record',
            json={
                'success': True,
                'unit_ids': [str(UNIT_ID)],
                'vault_id': str(FORBIDDEN_VAULT),
            },
        )
        assert resp.status_code == 200, resp.text
        mock_api.record_outcome.assert_awaited_once()


# ---------------------------------------------------------------------------
# No-auth (auth disabled)
# ---------------------------------------------------------------------------


class TestOutcomesNoAuth:
    def test_record_with_no_auth_passes_through(self, mock_api):
        client = _make_client(mock_api, auth=None)
        resp = client.post(
            '/api/v1/outcomes/record',
            json={
                'success': True,
                'unit_ids': [str(UNIT_ID)],
                'vault_id': str(FORBIDDEN_VAULT),
            },
        )
        assert resp.status_code == 200, resp.text
        mock_api.record_outcome.assert_awaited_once()
