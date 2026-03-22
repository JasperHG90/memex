"""End-to-end tests for the ACL/policy system.

Tests exercise the real server with auth enabled, verifying that each policy
(reader, writer, admin) can only access the endpoints allowed by its permissions.
"""

import json
import os
import secrets
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

from memex_core.server import app

# ---------------------------------------------------------------------------
# Keys (generated once per module)
# ---------------------------------------------------------------------------

ADMIN_KEY = secrets.token_urlsafe(32)
WRITER_KEY = secrets.token_urlsafe(32)
READER_KEY = secrets.token_urlsafe(32)


def _h(key: str) -> dict[str, str]:
    """Build X-API-Key header dict."""
    return {'X-API-Key': key}


def _set_env_vars(postgres_container: PostgresContainer) -> None:
    """Copy of conftest._set_env_vars — sets DB connection env vars."""
    from urllib.parse import urlparse

    dsn = postgres_container.get_connection_url()
    parsed = urlparse(dsn)

    os.environ['MEMEX_LOAD_LOCAL_CONFIG'] = 'false'
    os.environ['MEMEX_LOAD_GLOBAL_CONFIG'] = 'false'
    os.environ['MEMEX_SERVER__META_STORE__TYPE'] = 'postgres'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__HOST'] = parsed.hostname or 'localhost'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PORT'] = str(parsed.port or 5432)
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__DATABASE'] = parsed.path.lstrip('/')
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__USER'] = parsed.username or 'test'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD'] = parsed.password or 'test'
    os.environ['MEMEX_SERVER__MEMORY__REFLECTION__BACKGROUND_REFLECTION_ENABLED'] = 'false'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def auth_client(
    postgres_container: PostgresContainer,
    _truncate_db: None,
) -> TestClient:
    """TestClient with auth enabled and all three policy keys configured."""
    _set_env_vars(postgres_container)
    os.environ['MEMEX_SERVER__AUTH__ENABLED'] = 'true'
    os.environ['MEMEX_SERVER__AUTH__KEYS'] = json.dumps(
        [
            {'key': ADMIN_KEY, 'policy': 'admin', 'description': 'test-admin'},
            {'key': WRITER_KEY, 'policy': 'writer', 'description': 'test-writer'},
            {'key': READER_KEY, 'policy': 'reader', 'description': 'test-reader'},
        ]
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def noauth_client(
    postgres_container: PostgresContainer,
    _truncate_db: None,
) -> TestClient:
    """TestClient with auth disabled (default)."""
    _set_env_vars(postgres_container)
    with TestClient(app) as c:
        yield c


def _mock_ingest(client: TestClient, key: str, vault_id: str | None = None):
    """Ingest a note with mocked MemexAPI.ingest. Returns the response."""
    import base64
    from unittest.mock import AsyncMock

    uid = uuid4()
    note_name = f'Test Note {uid.hex[:8]}'
    content_text = f'Test content {uid}'
    body: dict = {
        'name': note_name,
        'description': 'ACL test note',
        'content': base64.b64encode(content_text.encode()).decode(),
        'tags': [],
    }
    if vault_id:
        body['vault_id'] = vault_id

    mock_result = {
        'status': 'success',
        'note_id': str(uid),
        'title': note_name,
        'vault_id': vault_id or 'global',
        'unit_ids': [],
        'entity_ids': [],
    }

    with patch('memex_core.api.MemexAPI.ingest', new_callable=AsyncMock, return_value=mock_result):
        resp = client.post('/api/v1/ingestions', json=body, headers=_h(key))
    return resp


# ---------------------------------------------------------------------------
# Test Group 1: Basic auth enforcement
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBasicAuth:
    """Verify basic 401/403 behavior."""

    def test_missing_key_returns_401(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/vaults')
        assert resp.status_code == 401

    def test_invalid_key_returns_403(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/vaults', headers=_h('bogus-key'))
        assert resp.status_code == 403

    def test_exempt_paths_bypass_auth(self, auth_client: TestClient):
        assert auth_client.get('/api/v1/health').status_code == 200
        assert auth_client.get('/api/v1/ready').status_code == 200


# ---------------------------------------------------------------------------
# Test Group 2: Admin key — full access
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAdminFullAccess:
    """Admin key should have unrestricted access."""

    def test_admin_full_workflow(self, auth_client: TestClient):
        # Create vault
        vault_name = f'admin-vault-{uuid4().hex[:8]}'
        resp = auth_client.post('/api/v1/vaults', json={'name': vault_name}, headers=_h(ADMIN_KEY))
        assert resp.status_code == 200
        vault_id = resp.json()['id']

        # Ingest note (mocked)
        resp = _mock_ingest(auth_client, ADMIN_KEY, vault_id)
        assert resp.status_code == 200

        # List notes (read)
        resp = auth_client.get('/api/v1/notes', headers=_h(ADMIN_KEY))
        assert resp.status_code == 200

        # Search (read)
        resp = auth_client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'vault_ids': [vault_id]},
            headers=_h(ADMIN_KEY),
        )
        assert resp.status_code == 200

        # Delete vault (delete permission)
        resp = auth_client.delete(f'/api/v1/vaults/{vault_id}', headers=_h(ADMIN_KEY))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test Group 3: Writer key — read + write, no delete
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestWriterPermissions:
    """Writer key can read and write, but cannot delete."""

    def test_writer_can_read(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/notes', headers=_h(WRITER_KEY))
        assert resp.status_code == 200

        resp = auth_client.post(
            '/api/v1/memories/search',
            json={'query': 'test'},
            headers=_h(WRITER_KEY),
        )
        assert resp.status_code == 200

        resp = auth_client.get('/api/v1/entities', headers=_h(WRITER_KEY))
        assert resp.status_code == 200

        resp = auth_client.get('/api/v1/stats/counts', headers=_h(WRITER_KEY))
        assert resp.status_code == 200

    def test_writer_can_write(self, auth_client: TestClient):
        # Create vault
        vault_name = f'writer-vault-{uuid4().hex[:8]}'
        resp = auth_client.post('/api/v1/vaults', json={'name': vault_name}, headers=_h(WRITER_KEY))
        assert resp.status_code == 200
        vault_id = resp.json()['id']

        # Ingest note
        resp = _mock_ingest(auth_client, WRITER_KEY, vault_id)
        assert resp.status_code == 200

    def test_writer_cannot_delete_note(self, auth_client: TestClient):
        # Writer should get 403 before the route checks if the note exists.
        fake_note_id = str(uuid4())
        resp = auth_client.delete(f'/api/v1/notes/{fake_note_id}', headers=_h(WRITER_KEY))
        assert resp.status_code == 403

    def test_writer_cannot_delete_vault(self, auth_client: TestClient):
        vault_name = f'wdv-vault-{uuid4().hex[:8]}'
        resp = auth_client.post('/api/v1/vaults', json={'name': vault_name}, headers=_h(ADMIN_KEY))
        vault_id = resp.json()['id']

        resp = auth_client.delete(f'/api/v1/vaults/{vault_id}', headers=_h(WRITER_KEY))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test Group 4: Reader key — read only
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestReaderPermissions:
    """Reader key can only read, not write or delete."""

    def test_reader_can_read(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/notes', headers=_h(READER_KEY))
        assert resp.status_code == 200

        resp = auth_client.post(
            '/api/v1/memories/search',
            json={'query': 'test'},
            headers=_h(READER_KEY),
        )
        assert resp.status_code == 200

        resp = auth_client.get('/api/v1/entities', headers=_h(READER_KEY))
        assert resp.status_code == 200

        resp = auth_client.get('/api/v1/vaults', headers=_h(READER_KEY))
        assert resp.status_code == 200

        resp = auth_client.get('/api/v1/stats/counts', headers=_h(READER_KEY))
        assert resp.status_code == 200

    def test_reader_cannot_write(self, auth_client: TestClient):
        import base64

        resp = auth_client.post(
            '/api/v1/ingestions',
            json={
                'name': 'Should Fail',
                'description': 'test',
                'content': base64.b64encode(b'test').decode(),
            },
            headers=_h(READER_KEY),
        )
        assert resp.status_code == 403

        resp = auth_client.post(
            '/api/v1/vaults',
            json={'name': 'should-fail'},
            headers=_h(READER_KEY),
        )
        assert resp.status_code == 403

        resp = auth_client.put(
            '/api/v1/kv',
            json={'key': 'test-key', 'value': 'test-value'},
            headers=_h(READER_KEY),
        )
        assert resp.status_code == 403

    def test_reader_cannot_delete(self, auth_client: TestClient):
        # Create a vault with admin to get a valid ID
        vault_name = f'rdel-vault-{uuid4().hex[:8]}'
        resp = auth_client.post('/api/v1/vaults', json={'name': vault_name}, headers=_h(ADMIN_KEY))
        vault_id = resp.json()['id']

        resp = auth_client.delete(f'/api/v1/vaults/{vault_id}', headers=_h(READER_KEY))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test Group 5: Auth disabled regression
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAuthDisabledRegression:
    """When auth is disabled, all endpoints should be accessible without keys."""

    def test_auth_disabled_allows_all(self, noauth_client: TestClient):
        # Read
        resp = noauth_client.get('/api/v1/vaults')
        assert resp.status_code == 200

        resp = noauth_client.get('/api/v1/notes')
        assert resp.status_code == 200

        resp = noauth_client.get('/api/v1/stats/counts')
        assert resp.status_code == 200

        # Write
        vault_name = f'noauth-vault-{uuid4().hex[:8]}'
        resp = noauth_client.post('/api/v1/vaults', json={'name': vault_name})
        assert resp.status_code == 200
        vault_id = resp.json()['id']

        # Delete
        resp = noauth_client.delete(f'/api/v1/vaults/{vault_id}')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test Group 6: Admin-only endpoints
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAdminOnlyEndpoints:
    """Audit and DLQ endpoints require admin policy."""

    def test_reader_blocked_from_audit(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/admin/audit', headers=_h(READER_KEY))
        assert resp.status_code == 403

    def test_writer_blocked_from_audit(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/admin/audit', headers=_h(WRITER_KEY))
        assert resp.status_code == 403

    def test_admin_can_access_audit(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/admin/audit', headers=_h(ADMIN_KEY))
        assert resp.status_code == 200

    def test_reader_blocked_from_dlq(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/admin/reflection/dlq', headers=_h(READER_KEY))
        assert resp.status_code == 403

    def test_admin_can_access_dlq(self, auth_client: TestClient):
        resp = auth_client.get('/api/v1/admin/reflection/dlq', headers=_h(ADMIN_KEY))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Test Group 7: Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestEdgeCases:
    """Edge cases for the ACL system."""

    def test_multiple_keys_same_policy(self, postgres_container, _truncate_db):
        """Multiple admin keys should both work."""
        key1 = secrets.token_urlsafe(32)
        key2 = secrets.token_urlsafe(32)

        _set_env_vars(postgres_container)
        os.environ['MEMEX_SERVER__AUTH__ENABLED'] = 'true'
        os.environ['MEMEX_SERVER__AUTH__KEYS'] = json.dumps(
            [
                {'key': key1, 'policy': 'admin'},
                {'key': key2, 'policy': 'admin'},
            ]
        )

        with TestClient(app) as client:
            assert client.get('/api/v1/vaults', headers=_h(key1)).status_code == 200
            assert client.get('/api/v1/vaults', headers=_h(key2)).status_code == 200

    def test_kv_permissions(self, auth_client: TestClient):
        """KV read/write/delete follows ACL."""
        # Reader can search KV
        resp = auth_client.post(
            '/api/v1/kv/search',
            json={'query': 'test'},
            headers=_h(READER_KEY),
        )
        assert resp.status_code == 200

        # Reader cannot write KV
        resp = auth_client.put(
            '/api/v1/kv',
            json={'key': 'should-fail', 'value': 'test'},
            headers=_h(READER_KEY),
        )
        assert resp.status_code == 403

        # Reader cannot delete KV
        resp = auth_client.delete(
            '/api/v1/kv/delete',
            params={'key': 'nonexistent'},
            headers=_h(READER_KEY),
        )
        assert resp.status_code == 403

        # Writer cannot delete KV
        resp = auth_client.delete(
            '/api/v1/kv/delete',
            params={'key': 'nonexistent'},
            headers=_h(WRITER_KEY),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Test Group 8: Vault-scoped access
# ---------------------------------------------------------------------------

SCOPED_READER_KEY = secrets.token_urlsafe(32)
SCOPED_WRITER_KEY = secrets.token_urlsafe(32)


@pytest.mark.integration
class TestVaultScopedAccess:
    """Tests that vault-scoped keys can only access their allowed vaults."""

    @pytest.fixture()
    def vault_scoped_client(
        self,
        postgres_container: PostgresContainer,
        _truncate_db: None,
    ):
        """Create two vaults and configure vault-scoped keys."""
        _set_env_vars(postgres_container)

        # First create vaults with an admin key
        os.environ['MEMEX_SERVER__AUTH__ENABLED'] = 'true'
        os.environ['MEMEX_SERVER__AUTH__KEYS'] = json.dumps(
            [
                {'key': ADMIN_KEY, 'policy': 'admin'},
                {
                    'key': SCOPED_READER_KEY,
                    'policy': 'reader',
                    'vault_ids': ['allowed-vault'],
                },
                {
                    'key': SCOPED_WRITER_KEY,
                    'policy': 'writer',
                    'vault_ids': ['allowed-vault'],
                },
            ]
        )
        with TestClient(app) as client:
            # Create the two vaults
            resp = client.post(
                '/api/v1/vaults',
                json={'name': 'allowed-vault'},
                headers=_h(ADMIN_KEY),
            )
            assert resp.status_code == 200
            allowed_vault_id = resp.json()['id']

            resp = client.post(
                '/api/v1/vaults',
                json={'name': 'restricted-vault'},
                headers=_h(ADMIN_KEY),
            )
            assert resp.status_code == 200
            restricted_vault_id = resp.json()['id']

            yield client, allowed_vault_id, restricted_vault_id

    def test_scoped_reader_can_read_allowed_vault(self, vault_scoped_client):
        client, allowed_id, _ = vault_scoped_client

        resp = client.get(
            '/api/v1/notes',
            params={'vault_id': 'allowed-vault'},
            headers=_h(SCOPED_READER_KEY),
        )
        assert resp.status_code == 200

        resp = client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'vault_ids': [allowed_id]},
            headers=_h(SCOPED_READER_KEY),
        )
        assert resp.status_code == 200

    def test_scoped_reader_blocked_from_other_vault(self, vault_scoped_client):
        client, _, restricted_id = vault_scoped_client

        resp = client.get(
            '/api/v1/notes',
            params={'vault_id': 'restricted-vault'},
            headers=_h(SCOPED_READER_KEY),
        )
        assert resp.status_code == 403

        resp = client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'vault_ids': [restricted_id]},
            headers=_h(SCOPED_READER_KEY),
        )
        assert resp.status_code == 403

    def test_scoped_reader_blocked_from_cross_vault_search(self, vault_scoped_client):
        client, allowed_id, restricted_id = vault_scoped_client

        resp = client.post(
            '/api/v1/memories/search',
            json={'query': 'test', 'vault_ids': [allowed_id, restricted_id]},
            headers=_h(SCOPED_READER_KEY),
        )
        assert resp.status_code == 403

    def test_scoped_writer_can_ingest_to_allowed_vault(self, vault_scoped_client):
        client, allowed_id, _ = vault_scoped_client

        resp = _mock_ingest(client, SCOPED_WRITER_KEY, allowed_id)
        assert resp.status_code == 200

    def test_scoped_writer_blocked_from_other_vault_ingest(self, vault_scoped_client):
        client, _, restricted_id = vault_scoped_client

        resp = _mock_ingest(client, SCOPED_WRITER_KEY, restricted_id)
        assert resp.status_code == 403
