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

    _INTERNAL_LEAK_NEEDLE = 'advisory lock id=0xdeadbeef'

    def test_consolidate_lock_timeout_returns_503_with_retry_after(self, mock_api):
        mock_api.consolidate_vault.side_effect = EntityLockTimeoutError(
            f'could not acquire {self._INTERNAL_LEAK_NEEDLE}'
        )
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/consolidate',
            json={'vault_id': str(ALLOWED_VAULT), 'dry_run': False},
        )
        assert resp.status_code == 503, resp.text
        assert resp.headers.get('retry-after') is not None
        assert int(resp.headers['retry-after']) > 0
        # Hermes round-3 MED: internal exception text MUST NOT leak to the
        # HTTP client. Detail is a generic, fixed string.
        body = resp.json()
        assert body['detail'] == 'Entity lock timeout — please retry shortly'
        assert self._INTERNAL_LEAK_NEEDLE not in body['detail']

    def test_consolidate_retry_after_derived_from_exception_timeout(self, mock_api):
        """Hermes round-4 MED: when ``EntityLockTimeoutError`` carries a
        ``timeout_seconds`` attribute, ``consolidate_vault`` MUST derive
        the ``Retry-After`` header from it (not hardcode ``'5'``)."""
        mock_api.consolidate_vault.side_effect = EntityLockTimeoutError(
            'could not acquire lock', timeout_seconds=12.0
        )
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/consolidate',
            json={'vault_id': str(ALLOWED_VAULT), 'dry_run': False},
        )
        assert resp.status_code == 503, resp.text
        assert resp.headers.get('retry-after') == '12'

    def test_consolidate_retry_after_falls_back_when_exception_lacks_timeout(self, mock_api):
        """Hermes round-4 MED: when the exception has no ``timeout_seconds``
        (legacy callsites), fall back to the ``'5'`` default."""
        mock_api.consolidate_vault.side_effect = EntityLockTimeoutError('could not acquire lock')
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/consolidate',
            json={'vault_id': str(ALLOWED_VAULT), 'dry_run': False},
        )
        assert resp.status_code == 503, resp.text
        assert resp.headers.get('retry-after') == '5'


class TestReconsolidateLockTimeoutEnvelope:
    """``reconsolidate_entity`` translates ``EntityLockTimeoutError`` into a
    503 with a ``Retry-After`` header (parity with ``consolidate_vault``).
    Hermes round-3 MED.
    """

    _INTERNAL_LEAK_NEEDLE = 'advisory lock id=0xfeedface'

    def test_reconsolidate_lock_timeout_returns_503_with_retry_after(self, mock_api):
        mock_api.reconsolidate_entity = AsyncMock(
            side_effect=EntityLockTimeoutError(f'could not acquire {self._INTERNAL_LEAK_NEEDLE}')
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
        assert resp.status_code == 503, resp.text
        # Hermes round-4 MED: Retry-After is derived from
        # request.timeout_seconds (not hardcoded). Here the request asked
        # for a 5s timeout, so Retry-After must be '5'.
        assert resp.headers.get('retry-after') == '5'
        body = resp.json()
        assert body['detail'] == 'Entity lock timeout — please retry shortly'
        # Hermes round-3 MED: internal exception text MUST NOT leak to the
        # HTTP client.
        assert self._INTERNAL_LEAK_NEEDLE not in body['detail']

    def test_reconsolidate_retry_after_matches_request_timeout(self, mock_api):
        """Hermes round-4 MED: ``Retry-After`` reflects the caller's
        ``timeout_seconds`` (e.g., a 30s request yields ``'30'``)."""
        mock_api.reconsolidate_entity = AsyncMock(
            side_effect=EntityLockTimeoutError('could not acquire lock', timeout_seconds=30.0)
        )
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/reconsolidate',
            json={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(ALLOWED_VAULT),
                'timeout_seconds': 30.0,
            },
        )
        assert resp.status_code == 503, resp.text
        assert resp.headers.get('retry-after') == '30'

    def test_reconsolidate_retry_after_uses_request_default_when_omitted(self, mock_api):
        """Hermes round-4 MED: when the client omits ``timeout_seconds``,
        Pydantic supplies the model default (30.0) and Retry-After reflects
        that — confirming the derivation is grounded in the request value
        and not the exception value when the request carries one."""
        mock_api.reconsolidate_entity = AsyncMock(
            side_effect=EntityLockTimeoutError('could not acquire lock')
        )
        client = _make_client(mock_api, _unrestricted_writer())
        resp = client.post(
            '/api/v1/memory/reconsolidate',
            json={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(ALLOWED_VAULT),
                # timeout_seconds omitted — should default to 30.0
            },
        )
        assert resp.status_code == 503, resp.text
        assert resp.headers.get('retry-after') == '30'

    def test_reconsolidate_lock_timeout_logs_full_exception(self, mock_api, caplog):
        """Server-side log MUST include the full exception so on-call can
        diagnose lock contention even though the HTTP detail is generic.
        """
        mock_api.reconsolidate_entity = AsyncMock(
            side_effect=EntityLockTimeoutError(f'could not acquire {self._INTERNAL_LEAK_NEEDLE}')
        )
        client = _make_client(mock_api, _unrestricted_writer())
        with caplog.at_level('WARNING', logger='memex.core.server'):
            resp = client.post(
                '/api/v1/memory/reconsolidate',
                json={
                    'entity_id': str(ENTITY_ID),
                    'vault_id': str(ALLOWED_VAULT),
                    'timeout_seconds': 5.0,
                },
            )
        assert resp.status_code == 503, resp.text
        warning_records = [r for r in caplog.records if r.levelname == 'WARNING']
        assert warning_records, 'Expected a WARNING log for entity lock timeout'
        assert any(self._INTERNAL_LEAK_NEEDLE in r.getMessage() for r in warning_records), (
            f'Expected internal lock-timeout detail in WARNING logs; got: '
            f'{[r.getMessage() for r in warning_records]}'
        )


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


# ---------------------------------------------------------------------------
# Schema drift: service-layer dict missing required keys must surface as a
# clearly-logged 500 ("schema mismatch"), NOT be swallowed by the broad
# service-error handler as a generic Internal Server Error. Hermes round-2 MED.
# ---------------------------------------------------------------------------


class TestReconsolidateResponseSchemaDrift:
    """If ``LocksService.reconsolidate_entity``'s return shape drifts from
    ``ReconsolidateResponse`` (e.g. a required field is removed at the
    service layer), the resulting ``pydantic.ValidationError`` must NOT be
    caught by the broad ``except (MemexError, ValueError, ...)`` block --
    that would silently surface a programming error as a generic 500 with
    no log signal indicating the real cause is response schema drift.

    Contract: response construction lives outside the service-error try
    block; a ValidationError from ``ReconsolidateResponse(**result)`` is
    logged at CRITICAL with a "schema drift" marker and surfaced as a 500
    with detail ``'Internal response schema mismatch'``.
    """

    def test_missing_required_key_logs_schema_drift_and_500(self, mock_api, caplog):
        # Service returns a dict missing required ``units_examined`` and
        # ``contradictions_run`` -- pure schema drift, not a business error.
        mock_api.reconsolidate_entity = AsyncMock(
            return_value={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(ALLOWED_VAULT),
                # 'units_examined': MISSING
                # 'contradictions_run': MISSING
                'mental_model_id': None,
                'observations_added': 0,
            }
        )
        client = _make_client(mock_api, _unrestricted_writer())
        with caplog.at_level('CRITICAL', logger='memex.core.server'):
            resp = client.post(
                '/api/v1/memory/reconsolidate',
                json={
                    'entity_id': str(ENTITY_ID),
                    'vault_id': str(ALLOWED_VAULT),
                    'timeout_seconds': 5.0,
                },
            )

        assert resp.status_code == 500, resp.text
        # Surfaced as a schema-mismatch 500, not the generic correlation-id envelope.
        assert resp.json()['detail'] == 'Internal response schema mismatch'

        # The "schema drift" marker MUST appear in the logs at CRITICAL --
        # this is the on-call signal that distinguishes a code bug from a
        # client-visible business error.
        critical_records = [r for r in caplog.records if r.levelname == 'CRITICAL']
        assert critical_records, 'Expected a CRITICAL log for schema drift'
        assert any('schema drift' in r.getMessage().lower() for r in critical_records), (
            f'Expected "schema drift" in CRITICAL logs; got: '
            f'{[r.getMessage() for r in critical_records]}'
        )


class TestReconsolidateAbandonedRoundTrip:
    """V18 round-8 H1 regression guard: ``abandoned: bool`` on
    ``ReconsolidateResponse`` must round-trip through pydantic and
    reach the HTTP JSON body. Without this test, a future refactor
    (or pydantic config change tightening unknown fields) could
    silently strip the field — re-opening the gap the round-8 fix
    plugged at the DTO boundary.
    """

    def test_abandoned_true_reaches_http_body(self, mock_api):
        mock_api.reconsolidate_entity = AsyncMock(
            return_value={
                'entity_id': str(ENTITY_ID),
                'vault_id': str(ALLOWED_VAULT),
                'units_examined': 3,
                'contradictions_run': 3,
                'mental_model_id': None,
                'observations_added': 0,
                'abandoned': True,
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
        assert body['abandoned'] is True, (
            'CAS-abandon signal must propagate through the DTO to the HTTP body; '
            'pre-V18-round-8 the field was silently stripped.'
        )

    def test_abandoned_false_default_on_success_path(self, mock_api):
        # The default fixture returns a dict WITHOUT ``abandoned``; the
        # default ``False`` must fill in (covers backward-compat for callers
        # that haven't been updated yet).
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
        assert body['abandoned'] is False
