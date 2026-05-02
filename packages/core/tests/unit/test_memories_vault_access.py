"""Per-vault auth scoping for the F4/F9 memories router (CRITICAL-001).

Each vault-scoped endpoint in ``server/memories.py`` MUST reject callers
whose AuthContext does not include the requested vault. Without these tests
a writer scoped to vault-A could deprioritize/restore/reconsolidate/
consolidate against vault-B (gap left when F4/F9 routes shipped without
``check_vault_access``).

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
from memex_core.services.locks import EntityLockTimeoutError


ALLOWED_VAULT = uuid4()
FORBIDDEN_VAULT = uuid4()
UNIT_ID = uuid4()
ENTITY_ID = uuid4()


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
    # F4 — UnitsService surface.
    api.deprioritize_memory_unit = AsyncMock()
    api.restore_memory_unit = AsyncMock()
    # F9 — locks/consolidate.
    api.reconsolidate_entity = AsyncMock(
        return_value={
            'entity_id': str(ENTITY_ID),
            'vault_id': str(ALLOWED_VAULT),
            'units_examined': 0,
            'contradictions_run': 0,
            'mental_model_id': None,
            'observations_added': 0,
        }
    )
    api.consolidate_vault = AsyncMock(return_value={'vault_id': str(ALLOWED_VAULT)})
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
    """Writer that may only access ALLOWED_VAULT."""
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
# Deny path: scoped key targeting a forbidden vault → 403, service not called.
# ---------------------------------------------------------------------------


class TestMemoriesForbiddenVault:
    def test_deprioritize_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            f'/api/v1/memories/{UNIT_ID}/deprioritize',
            json={'reason': 'noise', 'vault_id': str(FORBIDDEN_VAULT)},
        )
        assert resp.status_code == 403, resp.text
        mock_api.deprioritize_memory_unit.assert_not_called()

    def test_restore_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            f'/api/v1/memories/{UNIT_ID}/restore',
            json={'vault_id': str(FORBIDDEN_VAULT)},
        )
        assert resp.status_code == 403, resp.text
        mock_api.restore_memory_unit.assert_not_called()

    def test_reconsolidate_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            '/api/v1/memory/reconsolidate',
            json={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(FORBIDDEN_VAULT),
                'timeout_seconds': 5.0,
            },
        )
        assert resp.status_code == 403, resp.text
        mock_api.reconsolidate_entity.assert_not_called()

    def test_consolidate_blocks_forbidden_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            '/api/v1/memory/consolidate',
            json={'vault_id': str(FORBIDDEN_VAULT), 'dry_run': True},
        )
        assert resp.status_code == 403, resp.text
        mock_api.consolidate_vault.assert_not_called()

    def test_read_only_key_blocks_deprioritize_via_permission(self, mock_api):
        """A reader-policy key must be rejected by ``require_write`` *before*
        we ever look at the vault — defence in depth.
        """
        reader = AuthContext(
            key_prefix='test1234',
            key_name='reader',
            policy=Policy.READER,
            permissions=POLICY_PERMISSIONS[Policy.READER],
            vault_ids=[str(ALLOWED_VAULT)],
            read_vault_ids=None,
        )
        client = _make_client(mock_api, reader)
        resp = client.post(
            f'/api/v1/memories/{UNIT_ID}/deprioritize',
            json={'reason': 'noise', 'vault_id': str(ALLOWED_VAULT)},
        )
        assert resp.status_code == 403, resp.text
        mock_api.deprioritize_memory_unit.assert_not_called()


# ---------------------------------------------------------------------------
# Allow path: scoped key targeting its allowed vault → 200, service called.
# ---------------------------------------------------------------------------


class TestMemoriesAllowedVault:
    def test_reconsolidate_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            '/api/v1/memory/reconsolidate',
            json={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(ALLOWED_VAULT),
                'timeout_seconds': 5.0,
            },
        )
        assert resp.status_code == 200, resp.text
        mock_api.reconsolidate_entity.assert_awaited_once()

    def test_consolidate_allows_in_scope_vault(self, mock_api):
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            '/api/v1/memory/consolidate',
            json={'vault_id': str(ALLOWED_VAULT), 'dry_run': True},
        )
        assert resp.status_code == 200, resp.text
        mock_api.consolidate_vault.assert_awaited_once()

    def test_unrestricted_writer_can_reach_any_vault(self, mock_api):
        """``vault_ids=None`` (admin/unscoped key) bypasses the per-vault gate."""
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/consolidate',
            json={'vault_id': str(FORBIDDEN_VAULT), 'dry_run': True},
        )
        assert resp.status_code == 200, resp.text
        mock_api.consolidate_vault.assert_awaited_once()

    def test_deprioritize_passes_gate_for_in_scope_vault(self, mock_api):
        """Hard happy path — the gate doesn't refuse a request that respects
        the vault binding. The DTO build can fail downstream (mock has no
        fact_type) — we only assert the auth gate let it through and the
        service was invoked. The DTO-build smoke test lives in F4 integration
        tests, not here."""
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            f'/api/v1/memories/{UNIT_ID}/deprioritize',
            json={'reason': 'noise', 'vault_id': str(ALLOWED_VAULT)},
        )
        # Either 200 (full DTO returned) or 5xx (mock can't build the DTO);
        # the gate-bypass case (403) is the only failure mode that matters
        # here.
        assert resp.status_code != 403, resp.text
        mock_api.deprioritize_memory_unit.assert_awaited_once()


# ---------------------------------------------------------------------------
# No-auth (auth disabled): every endpoint is reachable.
# ---------------------------------------------------------------------------


class TestMemoriesNoAuth:
    def test_consolidate_with_no_auth_passes_through(self, mock_api):
        client = _make_client(mock_api, auth=None)
        resp = client.post(
            '/api/v1/memory/consolidate',
            json={'vault_id': str(FORBIDDEN_VAULT), 'dry_run': True},
        )
        assert resp.status_code == 200, resp.text
        mock_api.consolidate_vault.assert_awaited_once()


# ---------------------------------------------------------------------------
# Read-extra: read_vault_ids does NOT grant write — F4/F9 routes are WRITE.
# ---------------------------------------------------------------------------


class TestMemoriesReadExtras:
    def test_read_vault_ids_does_not_grant_write(self, mock_api):
        """A writer scoped to vault-A with read_vault_ids=[vault-B] cannot
        deprioritize against vault-B because deprioritize is a WRITE verb
        (`check_vault_access(..., permission=Permission.WRITE)`).
        """
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
            f'/api/v1/memories/{UNIT_ID}/deprioritize',
            json={'reason': 'noise', 'vault_id': str(FORBIDDEN_VAULT)},
        )
        assert resp.status_code == 403, resp.text
        mock_api.deprioritize_memory_unit.assert_not_called()


# ---------------------------------------------------------------------------
# Error envelope contracts: 503 with Retry-After header on lock timeout.
# ---------------------------------------------------------------------------


class TestConsolidateLockTimeoutEnvelope:
    """``consolidate_vault`` translates ``EntityLockTimeoutError`` into a 503
    with a ``Retry-After`` header so clients can back off (mirrors the F5
    summarize-node 429 contract and the note-append 503 contract).
    """

    def test_consolidate_lock_timeout_returns_503_with_retry_after(self, mock_api):
        mock_api.consolidate_vault.side_effect = EntityLockTimeoutError(
            'could not acquire advisory lock'
        )
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/consolidate',
            json={'vault_id': str(ALLOWED_VAULT), 'dry_run': False},
        )
        assert resp.status_code == 503, resp.text
        assert resp.headers.get('retry-after') is not None
        assert int(resp.headers['retry-after']) > 0


class TestReconsolidateMentalModelIdTyping:
    """``ReconsolidateResponse.mental_model_id`` is typed ``UUID | None`` —
    Pydantic must coerce the service-layer ``str`` UUID into a UUID instance
    in the JSON response (parity with ``entity_id`` and ``vault_id``).
    """

    def test_mental_model_id_is_returned_as_uuid_string(self, mock_api):
        mm_id = uuid4()
        mock_api.reconsolidate_entity = AsyncMock(
            return_value={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(ALLOWED_VAULT),
                'units_examined': 1,
                'contradictions_run': 1,
                'mental_model_id': str(mm_id),
                'observations_added': 2,
            }
        )
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/reconsolidate',
            json={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(ALLOWED_VAULT),
                'timeout_seconds': 5.0,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body['mental_model_id'] == str(mm_id)
        # Round-trips back through UUID() — proves Pydantic accepted/serialized as UUID.
        assert UUID(body['mental_model_id']) == mm_id

    def test_mental_model_id_none_serializes_as_null(self, mock_api):
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/reconsolidate',
            json={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(ALLOWED_VAULT),
                'timeout_seconds': 5.0,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()['mental_model_id'] is None
