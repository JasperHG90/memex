"""Tests for per-vault access annotations in the list_vaults endpoint."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from memex_common.config import Policy, POLICY_PERMISSIONS
from memex_core.server import app
from memex_core.server.auth import AuthContext, get_auth_context
from memex_core.server.common import get_api


VAULT_A_ID = uuid4()
VAULT_B_ID = uuid4()
VAULT_C_ID = uuid4()


def _parse_ndjson(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.config = SimpleNamespace(server=SimpleNamespace(default_active_vault='vault-a'))

    async def _resolve(identifier):
        mapping = {'vault-a': VAULT_A_ID, 'vault-b': VAULT_B_ID, 'vault-c': VAULT_C_ID}
        if isinstance(identifier, UUID):
            return identifier
        if identifier in mapping:
            return mapping[identifier]
        for name, uid in mapping.items():
            if str(uid) == str(identifier):
                return uid
        raise ValueError(f'Unknown vault: {identifier}')

    api.resolve_vault_identifier = AsyncMock(side_effect=_resolve)
    api.list_vaults_with_counts.return_value = [
        {
            'vault': SimpleNamespace(id=VAULT_A_ID, name='vault-a', description='Vault A'),
            'note_count': 10,
            'last_note_added_at': None,
        },
        {
            'vault': SimpleNamespace(id=VAULT_B_ID, name='vault-b', description='Vault B'),
            'note_count': 5,
            'last_note_added_at': None,
        },
        {
            'vault': SimpleNamespace(id=VAULT_C_ID, name='vault-c', description='Vault C'),
            'note_count': 0,
            'last_note_added_at': None,
        },
    ]
    return api


def _make_client(mock_api, auth_context: AuthContext | None):
    """Create a TestClient with the given mock API and auth context."""
    app.dependency_overrides[get_api] = lambda: mock_api
    app.dependency_overrides[get_auth_context] = lambda: auth_context
    client = TestClient(app)
    return client


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides = {}


class TestVaultAccessNoAuth:
    """When auth is disabled, access should be None."""

    def test_access_is_null_when_no_auth(self, mock_api):
        client = _make_client(mock_api, auth_context=None)
        response = client.get('/api/v1/vaults')
        assert response.status_code == 200
        data = _parse_ndjson(response.text)
        assert len(data) == 3
        for v in data:
            assert v['access'] is None


class TestVaultAccessUnrestrictedKey:
    """When vault_ids is None, key has access to all vaults with full policy perms."""

    def test_writer_all_vaults(self, mock_api):
        auth = AuthContext(
            key_prefix='test1234...',
            policy=Policy.WRITER,
            permissions=POLICY_PERMISSIONS[Policy.WRITER],
            vault_ids=None,
            read_vault_ids=None,
        )
        client = _make_client(mock_api, auth)
        response = client.get('/api/v1/vaults')
        assert response.status_code == 200
        data = _parse_ndjson(response.text)
        for v in data:
            assert v['access'] == ['read', 'write']

    def test_reader_all_vaults(self, mock_api):
        auth = AuthContext(
            key_prefix='test1234...',
            policy=Policy.READER,
            permissions=POLICY_PERMISSIONS[Policy.READER],
            vault_ids=None,
            read_vault_ids=None,
        )
        client = _make_client(mock_api, auth)
        response = client.get('/api/v1/vaults')
        assert response.status_code == 200
        data = _parse_ndjson(response.text)
        for v in data:
            assert v['access'] == ['read']

    def test_admin_all_vaults(self, mock_api):
        auth = AuthContext(
            key_prefix='test1234...',
            policy=Policy.ADMIN,
            permissions=POLICY_PERMISSIONS[Policy.ADMIN],
            vault_ids=None,
            read_vault_ids=None,
        )
        client = _make_client(mock_api, auth)
        response = client.get('/api/v1/vaults')
        assert response.status_code == 200
        data = _parse_ndjson(response.text)
        for v in data:
            assert v['access'] == ['delete', 'read', 'write']


class TestVaultAccessScopedKey:
    """When vault_ids is set, only those vaults get full permissions."""

    def test_scoped_writer_with_read_extras(self, mock_api):
        """Writer scoped to vault-a, with read access to vault-b. vault-c gets nothing."""
        auth = AuthContext(
            key_prefix='test1234...',
            policy=Policy.WRITER,
            permissions=POLICY_PERMISSIONS[Policy.WRITER],
            vault_ids=['vault-a'],
            read_vault_ids=['vault-b'],
        )
        client = _make_client(mock_api, auth)
        response = client.get('/api/v1/vaults')
        assert response.status_code == 200
        data = _parse_ndjson(response.text)

        by_name = {v['name']: v for v in data}
        assert by_name['vault-a']['access'] == ['read', 'write']
        assert by_name['vault-b']['access'] == ['read']
        assert by_name['vault-c']['access'] == []

    def test_scoped_reader_no_extras(self, mock_api):
        """Reader scoped to vault-b only."""
        auth = AuthContext(
            key_prefix='test1234...',
            policy=Policy.READER,
            permissions=POLICY_PERMISSIONS[Policy.READER],
            vault_ids=['vault-b'],
            read_vault_ids=None,
        )
        client = _make_client(mock_api, auth)
        response = client.get('/api/v1/vaults')
        assert response.status_code == 200
        data = _parse_ndjson(response.text)

        by_name = {v['name']: v for v in data}
        assert by_name['vault-b']['access'] == ['read']
        assert by_name['vault-a']['access'] == []
        assert by_name['vault-c']['access'] == []
