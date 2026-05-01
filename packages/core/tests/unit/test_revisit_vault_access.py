"""Per-vault auth scoping for the F20 revisit router (CRITICAL-001).

The revisit service already raises PermissionError on cross-vault review
(``services/revisitation.py:225``). This file verifies the *route-level*
gate fires first — the auth check rejects mismatched vaults before the
service is ever called.
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
    api.get_due_for_review = AsyncMock(return_value=[])
    api.review_memory_unit = AsyncMock(return_value={'unit_id': str(UNIT_ID)})
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


class TestRevisitForbiddenVault:
    def test_due_for_review_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/memory/due_for_review?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 403, resp.text
        mock_api.get_due_for_review.assert_not_called()

    def test_review_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            '/api/v1/memory/review',
            json={
                'unit_id': str(UNIT_ID),
                'quality': 3,
                'vault_id': str(FORBIDDEN_VAULT),
            },
        )
        assert resp.status_code == 403, resp.text
        mock_api.review_memory_unit.assert_not_called()


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


class TestRevisitAllowedVault:
    def test_due_for_review_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_reader())
        resp = client.get(f'/api/v1/memory/due_for_review?vault_id={ALLOWED_VAULT}')
        assert resp.status_code == 200, resp.text
        mock_api.get_due_for_review.assert_awaited_once()

    def test_review_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            '/api/v1/memory/review',
            json={
                'unit_id': str(UNIT_ID),
                'quality': 3,
                'vault_id': str(ALLOWED_VAULT),
            },
        )
        assert resp.status_code == 200, resp.text
        mock_api.review_memory_unit.assert_awaited_once()

    def test_unrestricted_key_can_reach_any_vault(self, mock_api):
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.get(f'/api/v1/memory/due_for_review?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text
        mock_api.get_due_for_review.assert_awaited_once()


# ---------------------------------------------------------------------------
# No-auth (auth disabled)
# ---------------------------------------------------------------------------


class TestRevisitNoAuth:
    def test_due_with_no_auth_passes_through(self, mock_api):
        client = _make_client(mock_api, auth=None)
        resp = client.get(f'/api/v1/memory/due_for_review?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Read-extra
# ---------------------------------------------------------------------------


class TestRevisitReadExtras:
    def test_read_vault_ids_grants_due_for_review(self, mock_api):
        """A writer scoped to vault-A with read_vault_ids=[vault-B] can hit
        the READ surface for vault-B."""
        auth = AuthContext(
            key_prefix='test1234',
            key_name='scoped-writer',
            policy=Policy.WRITER,
            permissions=frozenset({Permission.READ, Permission.WRITE}),
            vault_ids=[str(ALLOWED_VAULT)],
            read_vault_ids=[str(FORBIDDEN_VAULT)],
        )
        client = _make_client(mock_api, auth)
        resp = client.get(f'/api/v1/memory/due_for_review?vault_id={FORBIDDEN_VAULT}')
        assert resp.status_code == 200, resp.text

    def test_read_vault_ids_does_not_grant_review_write(self, mock_api):
        """``review`` is a WRITE verb — read_vault_ids must not unlock it."""
        auth = AuthContext(
            key_prefix='test1234',
            key_name='scoped-writer',
            policy=Policy.WRITER,
            permissions=frozenset({Permission.READ, Permission.WRITE}),
            vault_ids=[str(ALLOWED_VAULT)],
            read_vault_ids=[str(FORBIDDEN_VAULT)],
        )
        client = _make_client(mock_api, auth)
        resp = client.post(
            '/api/v1/memory/review',
            json={
                'unit_id': str(UNIT_ID),
                'quality': 3,
                'vault_id': str(FORBIDDEN_VAULT),
            },
        )
        assert resp.status_code == 403, resp.text
        mock_api.review_memory_unit.assert_not_called()
