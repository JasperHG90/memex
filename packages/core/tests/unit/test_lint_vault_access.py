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
    # ``lint_status?scope=all`` (default) and the resolve dispatcher both query
    # ``api.metastore.session().execute(...).scalar()/.mappings().first()`` —
    # stub a session that returns zero scalar and an empty mapping; per-test
    # overrides may replace ``api.metastore`` with a richer stub.
    metastore = AsyncMock()
    session_cm = AsyncMock()

    async def _execute(stmt, params=None):
        result = AsyncMock()
        result.scalar = lambda: 0
        mappings = AsyncMock()
        mappings.first = lambda: None
        result.mappings = lambda: mappings
        return result

    session_cm.execute = AsyncMock(side_effect=_execute)
    session_cm.__aenter__ = AsyncMock(return_value=session_cm)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    metastore.session = lambda: session_cm
    api.metastore = metastore
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


def _stub_metastore_finding(
    mock_api,
    *,
    rule_name: str = 'duplicate_notes',
    vault_id: str | None = None,
    evidence: dict | None = None,
    status: str = 'pending',
) -> None:
    """Stub ``api.metastore.session().execute(...)`` to return a synthetic
    maintenance_proposals row for the routes that load findings directly.
    """
    mock_api.metastore = AsyncMock()
    session_cm = AsyncMock()

    async def _execute(stmt, params=None):
        result = AsyncMock()
        mappings = AsyncMock()
        mappings.first = lambda: {
            'id': str(FINDING_ID),
            'vault_id': vault_id,
            'rule_name': rule_name,
            'target_id': str(uuid4()),
            'evidence': evidence or {},
            'status': status,
        }
        result.mappings = lambda: mappings
        return result

    session_cm.execute = AsyncMock(side_effect=_execute)
    session_cm.__aenter__ = AsyncMock(return_value=session_cm)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    mock_api.metastore.session = lambda: session_cm


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
        _stub_metastore_finding(mock_api, vault_id=str(FORBIDDEN_VAULT))
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
        _stub_metastore_finding(mock_api, vault_id=str(ALLOWED_VAULT))
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


class TestCollapseClusterEmptyVaultsAffected:
    """An entity_collapse_cluster finding with an empty ``vaults_affected``
    list must be rejected (400) at the dispatcher. Cross-vault destructive
    operations require an explicit scope; fail-open is unacceptable.
    """

    def _stub_finding(self, mock_api, vaults_affected: list[str]) -> None:
        mock_api.metastore = AsyncMock()
        session_cm = AsyncMock()

        async def _execute(stmt, params=None):
            result = AsyncMock()
            mappings = AsyncMock()
            mappings.first = lambda: {
                'id': str(FINDING_ID),
                'vault_id': None,
                'rule_name': 'entity_collapse_cluster',
                'target_id': str(uuid4()),
                'evidence': {
                    'cluster_members': [str(uuid4()), str(uuid4())],
                    'suggested_winner_id': str(uuid4()),
                    'vaults_affected': vaults_affected,
                },
                'status': 'pending',
            }
            result.mappings = lambda: mappings
            return result

        session_cm.execute = AsyncMock(side_effect=_execute)
        session_cm.__aenter__ = AsyncMock(return_value=session_cm)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        mock_api.metastore.session = lambda: session_cm

    def test_resolve_rejects_empty_vaults_affected(self, mock_api):
        self._stub_finding(mock_api, vaults_affected=[])
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            f'/api/v1/lint/findings/{FINDING_ID}/resolve',
            json={'winner_id': str(uuid4())},
        )
        assert resp.status_code == 400, resp.text
        assert 'vaults_affected' in resp.text
        mock_api.lint.set_status.assert_not_called()


class TestCollapseClusterEmptyBody:
    """A POST with no body to an entity_collapse_cluster finding MUST NOT
    bypass the rule-keyed dispatcher: the suggested winner should be applied,
    the empty-``vaults_affected`` guard should still run, and the multi-vault
    auth check should still gate the mutation.
    """

    def _stub_finding(
        self,
        mock_api,
        *,
        vaults_affected: list[str],
        cluster_members: list[str],
        suggested_winner_id: str,
    ) -> None:
        mock_api.metastore = AsyncMock()
        session_cm = AsyncMock()

        async def _execute(stmt, params=None):
            result = AsyncMock()
            mappings = AsyncMock()
            mappings.first = lambda: {
                'id': str(FINDING_ID),
                'vault_id': None,
                'rule_name': 'entity_collapse_cluster',
                'target_id': suggested_winner_id,
                'evidence': {
                    'cluster_members': cluster_members,
                    'suggested_winner_id': suggested_winner_id,
                    'vaults_affected': vaults_affected,
                },
                'status': 'pending',
            }
            result.mappings = lambda: mappings
            return result

        session_cm.execute = AsyncMock(side_effect=_execute)
        session_cm.__aenter__ = AsyncMock(return_value=session_cm)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        mock_api.metastore.session = lambda: session_cm

    def test_resolve_collapse_with_empty_body_applies_suggested_winner(self, mock_api):
        winner = str(uuid4())
        loser = str(uuid4())
        self._stub_finding(
            mock_api,
            vaults_affected=[str(ALLOWED_VAULT)],
            cluster_members=[winner, loser],
            suggested_winner_id=winner,
        )
        mock_api.entities = AsyncMock()
        mock_api.entities.collapse_cluster = AsyncMock(
            return_value={'winner_id': winner, 'losers_collapsed': 1}
        )
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/resolve')
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body['rule_name'] == 'entity_collapse_cluster'
        assert body['winner_id'] == winner
        assert body['winner_overridden'] is False
        mock_api.entities.collapse_cluster.assert_awaited_once()
        mock_api.lint.set_status.assert_awaited_once()

    def test_resolve_collapse_with_empty_body_still_enforces_empty_vaults_guard(self, mock_api):
        winner = str(uuid4())
        loser = str(uuid4())
        self._stub_finding(
            mock_api,
            vaults_affected=[],
            cluster_members=[winner, loser],
            suggested_winner_id=winner,
        )
        mock_api.entities = AsyncMock()
        mock_api.entities.collapse_cluster = AsyncMock()
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(f'/api/v1/lint/findings/{FINDING_ID}/resolve')
        assert resp.status_code == 400, resp.text
        assert 'vaults_affected' in resp.text
        mock_api.entities.collapse_cluster.assert_not_called()
        mock_api.lint.set_status.assert_not_called()


class TestCollapseClusterWinnerCanonicalNameAmbiguity:
    """If multiple cluster members share the same canonical_name, the
    --winner=<name> lookup MUST refuse to silently pick one — the operator
    sees one name in the CLI and must explicitly disambiguate with a UUID.
    """

    def _stub_finding_and_ambiguous_winner(
        self,
        mock_api,
        *,
        cluster_members: list[str],
        vaults_affected: list[str],
        winner_name: str,
        matching_ids: list[str],
    ) -> None:
        mock_api.metastore = AsyncMock()
        session_cm = AsyncMock()

        calls = {'n': 0}

        async def _execute(stmt, params=None):
            calls['n'] += 1
            result = AsyncMock()
            mappings = AsyncMock()
            stmt_text = str(stmt).lower()
            if 'maintenance_proposals' in stmt_text:
                mappings.first = lambda: {
                    'id': str(FINDING_ID),
                    'vault_id': None,
                    'rule_name': 'entity_collapse_cluster',
                    'target_id': cluster_members[0],
                    'evidence': {
                        'cluster_members': cluster_members,
                        'suggested_winner_id': cluster_members[0],
                        'vaults_affected': vaults_affected,
                    },
                    'status': 'pending',
                }
                result.mappings = lambda: mappings
                return result
            if 'from entities' in stmt_text and 'canonical_name' in stmt_text:
                mappings.all = lambda: [{'id': mid} for mid in matching_ids]
                mappings.first = lambda: ({'id': matching_ids[0]} if matching_ids else None)
                result.mappings = lambda: mappings
                return result
            mappings.first = lambda: None
            mappings.all = lambda: []
            result.mappings = lambda: mappings
            result.scalar = lambda: 0
            return result

        session_cm.execute = AsyncMock(side_effect=_execute)
        session_cm.__aenter__ = AsyncMock(return_value=session_cm)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        mock_api.metastore.session = lambda: session_cm

    def test_winner_canonical_name_ambiguity_rejected(self, mock_api):
        winner_a = str(uuid4())
        winner_b = str(uuid4())
        third = str(uuid4())
        self._stub_finding_and_ambiguous_winner(
            mock_api,
            cluster_members=[winner_a, winner_b, third],
            vaults_affected=[str(ALLOWED_VAULT)],
            winner_name='AcmeCorp',
            matching_ids=[winner_a, winner_b],
        )
        mock_api.entities = AsyncMock()
        mock_api.entities.collapse_cluster = AsyncMock()
        client = _make_client(mock_api, _scoped_writer())
        resp = client.post(
            f'/api/v1/lint/findings/{FINDING_ID}/resolve',
            json={'winner_canonical_name': 'AcmeCorp'},
        )
        assert resp.status_code == 400, resp.text
        assert 'ambiguous' in resp.text.lower()
        mock_api.entities.collapse_cluster.assert_not_called()
        mock_api.lint.set_status.assert_not_called()
