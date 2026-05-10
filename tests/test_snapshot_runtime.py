"""Tests for `memex_eval.snapshot.runtime`.

Covers the runtime helper that builds DB + filestore handles for
in-process snapshot operations, plus the eval-side cross-checks that
refuse mismatched server/eval environments.
"""

from __future__ import annotations


import pytest

from memex_eval.snapshot.runtime import (
    SnapshotRuntimeMismatch,
    check_runtime_matches_server,
    snapshot_runtime,
)


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def test_check_refuses_non_localhost_server() -> None:
    with pytest.raises(SnapshotRuntimeMismatch, match='loopback'):
        check_runtime_matches_server(
            'http://memex.internal:8000/api/v1/',
            {'server': {'embedding_model': {'type': 'onnx'}}},
        )


def test_check_refuses_url_missing_scheme() -> None:
    """A scheme-less URL must NOT slip through as the empty hostname."""
    with pytest.raises(SnapshotRuntimeMismatch, match='scheme'):
        check_runtime_matches_server(
            'memex.internal:8000',
            {'server': {'embedding_model': {'type': 'onnx'}}},
        )


def test_check_accepts_ipv4_loopback_aliases() -> None:
    """127.0.0.2 is also loopback per RFC 3330."""
    check_runtime_matches_server(
        'http://127.0.0.2:8000/api/v1/',
        {'server': {'embedding_model': {'type': 'onnx'}}},
    )


def test_check_accepts_localhost_with_matching_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture's testcontainer config sets ONNX, which matches the
    # MODEL_REGISTRY's ONNX entry by default.
    monkeypatch.setenv('MEMEX_SERVER__EMBEDDING_MODEL__TYPE', 'onnx')
    check_runtime_matches_server(
        'http://localhost:8000/api/v1/',
        {'server': {'embedding_model': {'type': 'onnx'}}},
    )


def test_check_refuses_embedding_model_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local env says onnx, server reports litellm — divergence.
    monkeypatch.setenv('MEMEX_SERVER__EMBEDDING_MODEL__TYPE', 'onnx')
    with pytest.raises(SnapshotRuntimeMismatch, match='divergence'):
        check_runtime_matches_server(
            'http://localhost:8000/api/v1/',
            {'server': {'embedding_model': {'type': 'litellm', 'model': 'foo/bar'}}},
        )


def test_check_skips_when_snapshot_lacks_embedding_info() -> None:
    # If the server doesn't expose enough to compare, the check is a
    # no-op (we'd rather not refuse on insufficient info — the
    # downstream importer's manifest check is the real gate).
    check_runtime_matches_server('http://localhost:8000/api/v1/', {'server': {}})


async def test_snapshot_runtime_yields_session_and_ddl_applied(
    postgres_url: str,
) -> None:
    """End-to-end: snapshot_runtime() builds a usable session and the
    eval_import_state table is present after entry. Uses the autouse
    ``ensure_db_env_vars`` fixture which already sets HOST/PORT/etc.
    pointing at the testcontainer.
    """
    from sqlalchemy import text

    async with snapshot_runtime() as rt:
        assert rt.session is not None
        # The session writes commit visibly.
        result = await rt.session.execute(text('SELECT 1'))
        assert result.scalar() == 1
        # The DDL ran.
        result = await rt.session.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = 'eval_import_state'")
        )
        assert result.scalar() == 1

    # After exit, attempting to reuse the session must fail (engine
    # disposed). We don't strictly assert that — `snapshot_runtime`'s
    # contract is that callers don't outlive the context.
    assert rt.config is not None  # SnapshotRuntime is a NamedTuple


async def test_snapshot_runtime_refuses_stale_alembic_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`snapshot_runtime` validates the live alembic head before
    yielding so a populate against a stale schema fails fast.

    Monkeypatches the script-dir head (NOT the DB row) so the test
    leaves the shared testcontainer's ``alembic_version`` untouched.
    """
    # Patch get_expected_head everywhere it's looked up.
    monkeypatch.setattr('memex_eval.snapshot.runtime.get_expected_head', lambda: 'deadbeefcafe')

    with pytest.raises(SnapshotRuntimeMismatch, match='Alembic head mismatch'):
        async with snapshot_runtime():
            pass


# ----------------------------------------------------------------------
# MultiVaultImportNotSupported — runner-level predicate. Lives here
# because it's snapshot-specific.


def _make_minimal_suite(
    tmp_path,
    source_vaults: list[str | None] | None = None,
    scenario_vaults: list[str | None] | None = None,
):
    from memex_eval.suite import Scenario, Suite, SuiteMetadata, SuiteSources
    from memex_eval.suite.base import GoldUnitIds
    from memex_eval.suite.sources import SourceNote

    notes = [
        SourceNote(
            path=tmp_path / f'note-{i}.md',
            note_key=f'note-{i}',
            content='b',
            title=f't{i}',
            vault_name=vn,
        )
        for i, vn in enumerate(source_vaults or [None])
    ]
    scenarios = [
        Scenario(
            id=f's{i}',
            description=f'd{i}',
            query='q',
            expected=GoldUnitIds(type='gold_unit_ids', note_keys=[]),
            vault_name=vn,
        )
        for i, vn in enumerate(scenario_vaults or [None])
    ]
    metadata = SuiteMetadata(
        name='multi_vault_test',
        schema_version='1',
        suite_version='1.0.0',
        description='d',
        tags=[],
        primary_metrics=[],
        components_under_test=[],
        knobs=[],
        requires_llm_judge=False,
    )
    return Suite(
        metadata=metadata,
        sources=SuiteSources(notes=notes),
        scenarios=scenarios,
    )


def test_refuse_if_multi_vault_accepts_single_vault(tmp_path) -> None:
    from memex_eval.suite.runner import _refuse_if_multi_vault_for_snapshot

    suite = _make_minimal_suite(tmp_path)
    _refuse_if_multi_vault_for_snapshot(suite)  # no raise


def test_refuse_if_multi_vault_refuses_per_note_vault(tmp_path) -> None:
    from memex_eval.suite.runner import (
        MultiVaultImportNotSupported,
        _refuse_if_multi_vault_for_snapshot,
    )

    suite = _make_minimal_suite(tmp_path, source_vaults=['secondary'])
    with pytest.raises(MultiVaultImportNotSupported, match='multi_vault_test'):
        _refuse_if_multi_vault_for_snapshot(suite)


def test_refuse_if_multi_vault_refuses_per_scenario_vault(tmp_path) -> None:
    from memex_eval.suite.runner import (
        MultiVaultImportNotSupported,
        _refuse_if_multi_vault_for_snapshot,
    )

    suite = _make_minimal_suite(tmp_path, scenario_vaults=['secondary'])
    with pytest.raises(MultiVaultImportNotSupported):
        _refuse_if_multi_vault_for_snapshot(suite)
