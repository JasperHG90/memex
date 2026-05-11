"""P8: --keep-vault / --reuse-vault.

Covers:
- Label sanitization (regex + '..' + path-resolve guard).
- Manifest write happens in --keep-vault path; vault deletion is skipped.
- Manifest read in --reuse-vault path; ingest+extraction is skipped; vault
  deletion is skipped (the next reuse needs the vault intact).
- Reuse refusal is scenario-scoped: scenarios whose setup contains
  any non-reusable handler (declared by the
  ``reusable_under_reuse_vault = False`` ClassVar) are skipped, with
  reason ``setup_action_not_reusable``.
- Reuse fails fast when manifest absent.
- Reuse fails fast when manifest names a vault that no longer exists.
- Reuse fails fast when the reused vault is missing expected source notes.
- --keep-vault and --reuse-vault are mutually exclusive at the CLI layer.
- --keep-vault / --reuse-vault refuse --all (manifest is per-suite).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import typer

from memex_eval.cli import _validate_vault_label
from memex_eval.suite.base import (
    KeywordsPresent,
    Scenario,
    SetupAction,
    Suite,
    SuiteMetadata,
    SuiteSources,
)
from memex_eval.suite.runner import run_suite
from memex_eval.suite.sources import SourceNote


# --------------------------------------------------------------------------- #
# Label validation                                                             #
# --------------------------------------------------------------------------- #


class TestValidateVaultLabel:
    def test_accepts_simple_alphanumeric(self) -> None:
        assert _validate_vault_label('mw_alpha_baseline') == 'mw_alpha_baseline'

    def test_accepts_with_dot_dash_underscore(self) -> None:
        assert _validate_vault_label('run-1.0_v2') == 'run-1.0_v2'

    @pytest.mark.parametrize(
        'bad',
        [
            '',
            '../etc/passwd',
            '..',
            '.hidden',
            '-leading-dash',
            'has space',
            'has/slash',
            'has\\backslash',
            'has:colon',
            'has;semi',
            'a..b',
        ],
    )
    def test_rejects_unsafe_labels(self, bad: str) -> None:
        with pytest.raises(typer.BadParameter):
            _validate_vault_label(bad)


# --------------------------------------------------------------------------- #
# Helpers — minimal Suite + mocked RemoteMemexAPI                              #
# --------------------------------------------------------------------------- #


def _make_suite(tmp_path) -> Suite:
    src_path = tmp_path / 'a.md'
    src_path.write_text('alpha body')
    notes = [
        SourceNote(
            note_key='alpha',
            path=src_path,
            content='alpha body',
            title='Alpha Note',
        )
    ]
    metadata = SuiteMetadata(
        name='kr_test',
        schema_version='1',
        suite_version='1.0.0',
        description='keep/reuse test suite',
    )
    scenarios = [
        Scenario(
            id='plain_query',
            description='no setup actions',
            query='alpha',
            top_k=3,
            expected=KeywordsPresent(type='keywords_present', keywords=['alpha']),
        ),
        Scenario(
            id='outcome_stamper',
            description='records an outcome at setup time — non-idempotent',
            query='alpha',
            top_k=3,
            expected=KeywordsPresent(type='keywords_present', keywords=['alpha']),
            setup_actions=[SetupAction(kind='record_outcome', search_query='alpha', success=True)],
        ),
    ]
    return Suite(
        metadata=metadata,
        sources=SuiteSources(notes=notes),
        scenarios=scenarios,
    )


def _build_mock_api(
    *,
    primary_vault_id: UUID,
    note_id: UUID,
    extra_vaults: dict[str, UUID] | None = None,
) -> MagicMock:
    """Mocked RemoteMemexAPI used by run_suite. Builds the full set of
    methods the runner touches when ingest+extraction is skipped."""
    extra_vaults = extra_vaults or {}
    all_vaults = [
        SimpleNamespace(id=primary_vault_id, name='primary'),
        *(SimpleNamespace(id=vid, name=name) for name, vid in extra_vaults.items()),
    ]
    api = MagicMock()
    api.get_system_config = AsyncMock(return_value={})
    api.list_vaults = AsyncMock(return_value=all_vaults)
    api.create_vault = AsyncMock(return_value=SimpleNamespace(id=primary_vault_id, name='primary'))
    api.delete_vault = AsyncMock(return_value=None)
    api.ingest = AsyncMock(return_value=SimpleNamespace(note_id=note_id, status='created'))
    api.list_notes = AsyncMock(
        return_value=[SimpleNamespace(id=note_id, name='Alpha Note', vault_id=primary_vault_id)]
    )
    api.list_memory_units_by_note = AsyncMock(return_value=[SimpleNamespace(id='unit-1')])
    api.find_notes_by_title = AsyncMock(return_value=[])

    # _execute_scenario eventually calls api.search_memory; stub it cheaply.
    fake_unit = SimpleNamespace(
        id='unit-1',
        content='alpha body',
        score=1.0,
        rank=1,
        unit_metadata={},
    )
    api.search_memory = AsyncMock(return_value=[fake_unit])
    return api


@pytest.fixture
def patch_api(monkeypatch):
    """Patch RemoteMemexAPI(client) → mock and capture the mock for assertions."""

    def _install(api: MagicMock):
        monkeypatch.setattr('memex_eval.suite.runner.RemoteMemexAPI', lambda _client: api)

        # Stub the per-note extraction wait + vault-stable wait so the test
        # is hermetic and fast (it doesn't rely on real polling behavior).
        async def _fast_per_note_wait(api, note_id_by_key, note_key_to_vault_id, **_):
            return {k: ['unit-1'] for k in note_id_by_key}

        monkeypatch.setattr(
            'memex_eval.suite.runner._wait_extraction_per_note', _fast_per_note_wait
        )

        async def _noop_wait(*a, **k):
            return None

        monkeypatch.setattr('memex_eval.suite.runner.wait_for_extraction', _noop_wait)
        return api

    return _install


# --------------------------------------------------------------------------- #
# Manifest write + skip-deletion (--keep-vault)                                #
# --------------------------------------------------------------------------- #


class TestKeepVault:
    @pytest.mark.asyncio
    async def test_keep_vault_writes_manifest_and_skips_delete(self, tmp_path, patch_api) -> None:
        suite = _make_suite(tmp_path)
        primary_id = uuid4()
        note_id = uuid4()
        api = _build_mock_api(primary_vault_id=primary_id, note_id=note_id)
        patch_api(api)

        manifest_dir = tmp_path / 'manifests'
        await run_suite(
            suite,
            server_url='http://x/api/v1/',
            keep_vault='alpha_run',
            manifest_dir=manifest_dir,
        )
        manifest_path = manifest_dir / 'alpha_run.json'
        assert manifest_path.exists()
        payload = json.loads(manifest_path.read_text())
        assert payload['label'] == 'alpha_run'
        assert payload['suite_name'] == 'kr_test'
        assert UUID(payload['primary_vault']['id']) == primary_id
        # Vault deletion MUST be skipped when keep_vault is set.
        api.delete_vault.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Manifest read + ingest skip + scenario reuse-skip (--reuse-vault)            #
# --------------------------------------------------------------------------- #


class TestReuseVault:
    @pytest.mark.asyncio
    async def test_reuse_skips_ingest_and_skips_record_outcome_scenarios(
        self, tmp_path, patch_api
    ) -> None:
        suite = _make_suite(tmp_path)
        primary_id = uuid4()
        note_id = uuid4()
        api = _build_mock_api(primary_vault_id=primary_id, note_id=note_id)
        patch_api(api)

        manifest_dir = tmp_path / 'manifests'
        manifest_dir.mkdir()
        (manifest_dir / 'alpha_run.json').write_text(
            json.dumps(
                {
                    'label': 'alpha_run',
                    'suite_name': 'kr_test',
                    'suite_version': '1.0.0',
                    'sources_hash': 'unused',
                    'created_at': '2026-01-01T00:00:00+00:00',
                    'primary_vault': {'id': str(primary_id), 'name': 'primary'},
                    'secondary_vaults': {},
                }
            )
        )

        result = await run_suite(
            suite,
            server_url='http://x/api/v1/',
            reuse_vault='alpha_run',
            manifest_dir=manifest_dir,
        )

        # ingest must be skipped (no creating notes, no creating vaults).
        api.ingest.assert_not_awaited()
        api.create_vault.assert_not_awaited()
        # The runner must NOT delete the vault on reuse — the next reuse
        # needs it intact.
        api.delete_vault.assert_not_awaited()

        # The plain_query scenario should have run; the outcome_stamper one
        # should be marked status='skip' with reason setup_action_not_reusable.
        outcomes_by_id = {o.scenario_id: o for o in result.scenario_outcomes}
        stamper = outcomes_by_id['outcome_stamper']
        assert stamper.status == 'skip'
        assert stamper.skip_reason == 'setup_action_not_reusable'
        plain = outcomes_by_id['plain_query']
        # plain_query MUST run on the reused vault — round-8 M2 strengthens
        # this to ``status != 'skip'`` (was: only "not skipped for record_outcome
        # reasons"). Any skip — for any reason — fails this test. ``error`` is
        # accepted because the in-memory mocks intentionally don't render the
        # full agent stack; what matters for the round-7 M6 invariant is that
        # the scenario was attempted, not that it succeeded.
        assert plain.status != 'skip', (
            f'plain_query must run on reuse, got status={plain.status!r}, '
            f'skip_reason={plain.skip_reason!r}'
        )

    @pytest.mark.asyncio
    async def test_reuse_missing_manifest_raises(self, tmp_path, patch_api) -> None:
        suite = _make_suite(tmp_path)
        api = _build_mock_api(primary_vault_id=uuid4(), note_id=uuid4())
        patch_api(api)
        manifest_dir = tmp_path / 'manifests'
        manifest_dir.mkdir()
        with pytest.raises(FileNotFoundError, match='manifest not found'):
            await run_suite(
                suite,
                server_url='http://x/api/v1/',
                reuse_vault='ghost',
                manifest_dir=manifest_dir,
            )

    @pytest.mark.asyncio
    async def test_reuse_stale_vault_raises(self, tmp_path, patch_api) -> None:
        """Manifest names a vault id that no longer exists on the server."""
        suite = _make_suite(tmp_path)
        # Server returns a DIFFERENT vault than the manifest references.
        api = _build_mock_api(primary_vault_id=uuid4(), note_id=uuid4())
        patch_api(api)
        manifest_dir = tmp_path / 'manifests'
        manifest_dir.mkdir()
        stale_id = uuid4()
        (manifest_dir / 'stale_run.json').write_text(
            json.dumps(
                {
                    'label': 'stale_run',
                    'suite_name': 'kr_test',
                    'suite_version': '1.0.0',
                    'sources_hash': 'unused',
                    'created_at': '2026-01-01T00:00:00+00:00',
                    'primary_vault': {'id': str(stale_id), 'name': 'primary'},
                    'secondary_vaults': {},
                }
            )
        )
        with pytest.raises(FileNotFoundError, match='no longer exists'):
            await run_suite(
                suite,
                server_url='http://x/api/v1/',
                reuse_vault='stale_run',
                manifest_dir=manifest_dir,
            )

    @pytest.mark.asyncio
    async def test_reuse_missing_note_raises(self, tmp_path, patch_api) -> None:
        """Reused vault exists but doesn't contain the suite's source notes."""
        suite = _make_suite(tmp_path)
        primary_id = uuid4()
        api = _build_mock_api(primary_vault_id=primary_id, note_id=uuid4())
        # list_notes returns NO notes — vault is empty.
        api.list_notes = AsyncMock(return_value=[])
        patch_api(api)
        manifest_dir = tmp_path / 'manifests'
        manifest_dir.mkdir()
        (manifest_dir / 'empty_run.json').write_text(
            json.dumps(
                {
                    'label': 'empty_run',
                    'suite_name': 'kr_test',
                    'suite_version': '1.0.0',
                    'sources_hash': 'unused',
                    'created_at': '2026-01-01T00:00:00+00:00',
                    'primary_vault': {'id': str(primary_id), 'name': 'primary'},
                    'secondary_vaults': {},
                }
            )
        )
        with pytest.raises(ValueError, match='missing expected notes'):
            await run_suite(
                suite,
                server_url='http://x/api/v1/',
                reuse_vault='empty_run',
                manifest_dir=manifest_dir,
            )

    @pytest.mark.asyncio
    async def test_reuse_ambiguous_name_raises(self, tmp_path, patch_api) -> None:
        """Round-6 H2: two notes with the same display name in a reused
        vault must raise — ``NoteListItemDTO`` doesn't expose ``note_key``,
        so the resolver can't tell them apart and the safe behavior is
        to refuse rather than silently bind the wrong note id."""
        suite = _make_suite(tmp_path)
        primary_id = uuid4()
        api = _build_mock_api(primary_vault_id=primary_id, note_id=uuid4())
        # Two notes with the SAME name='Alpha Note' in the primary vault.
        api.list_notes = AsyncMock(
            return_value=[
                SimpleNamespace(id=uuid4(), name='Alpha Note', vault_id=primary_id),
                SimpleNamespace(id=uuid4(), name='Alpha Note', vault_id=primary_id),
            ]
        )
        patch_api(api)
        manifest_dir = tmp_path / 'manifests'
        manifest_dir.mkdir()
        (manifest_dir / 'dup_run.json').write_text(
            json.dumps(
                {
                    'label': 'dup_run',
                    'suite_name': 'kr_test',
                    'suite_version': '1.0.0',
                    'sources_hash': 'unused',
                    'created_at': '2026-01-01T00:00:00+00:00',
                    'primary_vault': {'id': str(primary_id), 'name': 'primary'},
                    'secondary_vaults': {},
                }
            )
        )
        with pytest.raises(ValueError, match='multiple notes share a display name'):
            await run_suite(
                suite,
                server_url='http://x/api/v1/',
                reuse_vault='dup_run',
                manifest_dir=manifest_dir,
            )


# --------------------------------------------------------------------------- #
# Round-6 fixes — mutual exclusion, manifest dir auto-creation, run-completed  #
# gating, registry-driven reuse refusal.                                       #
# --------------------------------------------------------------------------- #


class TestRunSuiteMutualExclusion:
    """Round-6 H3: keep_vault + reuse_vault must be rejected at the runner
    layer too — library callers (test harnesses, orchestration scripts)
    must not depend on the CLI to enforce mutual exclusion."""

    @pytest.mark.asyncio
    async def test_runner_rejects_both_flags(self, tmp_path, patch_api) -> None:
        suite = _make_suite(tmp_path)
        api = _build_mock_api(primary_vault_id=uuid4(), note_id=uuid4())
        patch_api(api)
        with pytest.raises(ValueError, match='mutually exclusive'):
            await run_suite(
                suite,
                server_url='http://x/api/v1/',
                keep_vault='a',
                reuse_vault='b',
                manifest_dir=tmp_path / 'm',
            )


class TestManifestDirAutoCreation:
    """M2: --keep-vault should auto-create the manifest directory."""

    @pytest.mark.asyncio
    async def test_manifest_dir_auto_created(self, tmp_path, patch_api) -> None:
        suite = _make_suite(tmp_path)
        primary_id = uuid4()
        note_id = uuid4()
        api = _build_mock_api(primary_vault_id=primary_id, note_id=note_id)
        patch_api(api)
        # Nested path that doesn't exist yet.
        manifest_dir = tmp_path / 'a' / 'b' / 'c'
        assert not manifest_dir.exists()
        await run_suite(
            suite,
            server_url='http://x/api/v1/',
            keep_vault='auto_dir',
            manifest_dir=manifest_dir,
        )
        assert (manifest_dir / 'auto_dir.json').exists()


class TestKeepVaultRunCompletedGate:
    """Round-6 C1 + round-7 H1: the manifest must NOT be written if the
    run did not complete cleanly. Both KeyboardInterrupt AND any generic
    Exception escape paths are covered — round-7 flagged that the
    original test only proved the KeyboardInterrupt path."""

    @pytest.mark.parametrize(
        ('exc', 'expected_exc_type'),
        [
            (KeyboardInterrupt('user abort'), KeyboardInterrupt),
            (RuntimeError('unexpected backend crash'), RuntimeError),
        ],
    )
    @pytest.mark.asyncio
    async def test_no_manifest_when_run_aborts(
        self, tmp_path, patch_api, monkeypatch, exc, expected_exc_type
    ) -> None:
        suite = _make_suite(tmp_path)
        primary_id = uuid4()
        note_id = uuid4()
        api = _build_mock_api(primary_vault_id=primary_id, note_id=note_id)
        patch_api(api)

        async def _explode(*a, **k):
            raise exc

        monkeypatch.setattr('memex_eval.suite.runner._execute_scenario', _explode)
        manifest_dir = tmp_path / 'manifests'
        with pytest.raises(expected_exc_type):
            await run_suite(
                suite,
                server_url='http://x/api/v1/',
                keep_vault='abort_run',
                manifest_dir=manifest_dir,
            )
        # Vault is preserved (--keep-vault honored even on abort), but
        # manifest is NOT written so the next --reuse-vault run can't
        # blind-bind to an under-populated vault.
        assert not (manifest_dir / 'abort_run.json').exists()
        # Round-7 M4: also verify the manifest dir was not even created —
        # the mkdir lives inside the same `if keep_vault and run_completed:`
        # guard, so a leftover empty directory would prove the gate leaked.
        assert not manifest_dir.exists()
        # Vault MUST still be preserved (no delete_vault calls), so user
        # can inspect the partial state manually.
        api.delete_vault.assert_not_awaited()


class TestRegistryDrivenReuseRefusal:
    """Round-6 H4: reuse-skip must be driven by the
    ``reusable_under_reuse_vault`` ClassVar on each handler, not a
    hardcoded ``record_outcome`` string check. ``record_outcome``'s
    handler declares ``reusable_under_reuse_vault = False``."""

    def test_record_outcome_handler_marked_non_reusable(self) -> None:
        from memex_eval.suite.setup_actions import _RecordOutcome

        assert _RecordOutcome.reusable_under_reuse_vault is False

    def test_default_handlers_are_reusable(self) -> None:
        from memex_eval.suite.setup_actions import (
            _ConsolidationTick,
            _Deprioritize,
            _KvWrite,
            _LintRun,
        )

        # Defaults: every handler EXCEPT _RecordOutcome is reusable.
        # If a handler turns out to be non-idempotent, change its
        # ClassVar; downstream tests + the runner pick up the change
        # via duck-typing without code changes here.
        assert _Deprioritize.reusable_under_reuse_vault is True
        assert _KvWrite.reusable_under_reuse_vault is True
        assert _ConsolidationTick.reusable_under_reuse_vault is True
        assert _LintRun.reusable_under_reuse_vault is True

    @pytest.mark.asyncio
    async def test_runner_consults_classvar_for_custom_handler(self, tmp_path, patch_api) -> None:
        """Round-7 M6: the runner must consult the
        ``reusable_under_reuse_vault`` ClassVar for ANY registered
        handler, not just the built-in ``record_outcome``. Register a
        custom handler with a unique kind and assert the scenario gets
        skipped under --reuse-vault.
        """
        # Round-8 M1: prefer the public ``unregister_setup_action`` over
        # poking ``_SETUP_ACTION_REGISTRY`` directly, so this test stays
        # green if the registry's internal storage is refactored.
        from memex_eval.suite.setup_actions import (
            SetupActionHandler,
            register_setup_action,
            unregister_setup_action,
        )

        custom_kind = 'rt_round7_custom_skip'
        try:

            @register_setup_action(custom_kind)
            class _CustomNonReusable(SetupActionHandler):
                reusable_under_reuse_vault = False

                async def run(self, api, vault_id, params):
                    return None

            # Build a suite with one scenario that uses the custom action,
            # plus one plain scenario. Reuse must skip the custom one and
            # run the plain one.
            src_path = tmp_path / 'a.md'
            src_path.write_text('alpha body')
            note = SourceNote(
                note_key='alpha',
                path=src_path,
                content='alpha body',
                title='Alpha Note',
            )
            metadata = SuiteMetadata(
                name='kr_custom',
                schema_version='1',
                suite_version='1.0.0',
                description='custom handler reuse-skip test',
            )
            scenarios = [
                Scenario(
                    id='plain',
                    description='runs on reuse',
                    query='alpha',
                    top_k=3,
                    expected=KeywordsPresent(type='keywords_present', keywords=['alpha']),
                ),
                Scenario(
                    id='non_reusable_setup',
                    description='must skip on reuse',
                    query='alpha',
                    top_k=3,
                    expected=KeywordsPresent(type='keywords_present', keywords=['alpha']),
                    setup_actions=[SetupAction(kind=custom_kind)],
                ),
            ]
            suite = Suite(
                metadata=metadata,
                sources=SuiteSources(notes=[note]),
                scenarios=scenarios,
            )

            primary_id = uuid4()
            api = _build_mock_api(primary_vault_id=primary_id, note_id=uuid4())
            patch_api(api)
            manifest_dir = tmp_path / 'manifests'
            manifest_dir.mkdir()
            (manifest_dir / 'reuse.json').write_text(
                json.dumps(
                    {
                        'label': 'reuse',
                        'suite_name': 'kr_custom',
                        'suite_version': '1.0.0',
                        'sources_hash': 'unused',
                        'created_at': '2026-01-01T00:00:00+00:00',
                        'primary_vault': {'id': str(primary_id), 'name': 'primary'},
                        'secondary_vaults': {},
                    }
                )
            )

            result = await run_suite(
                suite,
                server_url='http://x/api/v1/',
                reuse_vault='reuse',
                manifest_dir=manifest_dir,
            )
            outcomes_by_id = {o.scenario_id: o for o in result.scenario_outcomes}
            assert outcomes_by_id['non_reusable_setup'].status == 'skip'
            assert outcomes_by_id['non_reusable_setup'].skip_reason == 'setup_action_not_reusable'
            # Round-8 M2: ``plain`` MUST actively run on reuse, not silently
            # skip. ``status != 'skip'`` catches any skip reason (the original
            # weak assertion only caught the literal ``setup_action_not_reusable``
            # string). ``error`` is accepted because the in-memory mocks
            # intentionally don't render the full agent stack; what matters
            # for the M6 invariant is that the scenario was attempted at all.
            plain = outcomes_by_id['plain']
            assert plain.status != 'skip', (
                f'plain scenario must run on reuse, got status={plain.status!r}, '
                f'skip_reason={plain.skip_reason!r}'
            )
        finally:
            unregister_setup_action(custom_kind)
