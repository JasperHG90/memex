"""Unit tests for migration 033_confidence_evidence_count (F22).

Static checks + a behavioural test for the chunked-backfill loop's
termination condition. The full round-trip (column appears on upgrade,
disappears on downgrade, backfill matches the contradicts/weakens count)
runs in the integration suite via the testcontainer fixture — see
``test_int_f22_confidence_composition.py``.
"""

from __future__ import annotations

import importlib.util
import pathlib as plb
import re
from typing import Any
from unittest.mock import MagicMock, patch


def _load_migration_033() -> Any:
    import memex_core

    package_dir = plb.Path(memex_core.__file__).resolve().parent
    migration_path = package_dir / 'alembic' / 'versions' / '033_confidence_evidence_count.py'
    spec = importlib.util.spec_from_file_location('migration_033', migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_033_source() -> str:
    import memex_core

    package_dir = plb.Path(memex_core.__file__).resolve().parent
    migration_path = package_dir / 'alembic' / 'versions' / '033_confidence_evidence_count.py'
    return migration_path.read_text(encoding='utf-8')


class TestMigration033Metadata:
    def test_revision_id_fits_in_alembic_version_column(self):
        m = _load_migration_033()
        assert len(m.revision) <= 32
        assert m.revision == '033_confidence_evidence_count'

    def test_down_revision_chains_from_032(self):
        m = _load_migration_033()
        assert m.down_revision == '032_fsfm_decay_columns'


class TestMigration033SqlShape:
    def test_backfill_includes_order_by_for_deterministic_progress(self):
        source = _migration_033_source()
        assert re.search(r'ORDER BY ml\.to_unit_id', source), (
            'Backfill subquery must ORDER BY to_unit_id so batches are '
            'deterministic for progress logging.'
        )

    def test_backfill_filters_link_types(self):
        source = _migration_033_source()
        assert re.search(r"link_type IN \('contradicts', 'weakens'\)", source), (
            'Backfill must count contradicts + weakens only (reinforces '
            'is excluded by F22 v1 design).'
        )

    def test_backfill_idempotent_filter(self):
        source = _migration_033_source()
        assert re.search(r'mu_inner\.confidence_evidence_count = 0', source), (
            'Inner candidate selection must filter on confidence_evidence_count = 0 '
            'so a re-run is a no-op.'
        )

    def test_backfill_index_created_before_loop(self):
        source = _migration_033_source()
        index_pos = source.find('_BACKFILL_INDEX_NAME')
        loop_pos = source.find('while True')
        assert index_pos != -1 and loop_pos != -1
        assert index_pos < loop_pos, (
            'Composite index must be created before the chunked loop so each batch '
            'range-scans instead of seq-scanning memory_links.'
        )


class TestChunkedBackfillTermination:
    """Pin the chunked-loop's exit condition: stop when a batch updates 0 rows."""

    def _run_upgrade_with_mocked_execute(self, rowcounts: list[int]) -> int:
        m = _load_migration_033()
        # Each backfill batch result returns a list whose length the
        # production code reads via ``len(result.all())`` — driver-independent
        # rowcount (Hermes round-18 MED). One trailing result is the
        # mismatch-verification SELECT (``.scalar() -> 0`` = no mismatches).
        backfill_results: list[MagicMock] = []
        for rc in rowcounts:
            r = MagicMock()
            r.all.return_value = [object()] * rc
            backfill_results.append(r)

        verification_result = MagicMock()
        verification_result.scalar.return_value = 0

        bind = MagicMock()
        bind.execute.side_effect = backfill_results + [verification_result]

        with (
            patch.object(m.op, 'get_bind', return_value=bind),
            patch.object(m.op, 'add_column'),
            patch.object(m.op, 'create_index'),
            patch.object(m.op, 'create_check_constraint'),
            patch.object(m, '_column_exists', return_value=True),
            patch.object(m, '_constraint_exists', return_value=True),
            patch.object(m, '_index_exists', return_value=True),
        ):
            m.upgrade()

        # Subtract 1 for the trailing verification call so existing
        # assertions on backfill call count still hold.
        return bind.execute.call_count - 1

    def test_loop_exits_on_zero_rowcount(self):
        calls = self._run_upgrade_with_mocked_execute([5000, 5000, 1234, 0])
        assert calls == 4, 'Loop must run until a batch returns 0 rows'

    def test_loop_exits_on_first_zero_rowcount(self):
        calls = self._run_upgrade_with_mocked_execute([0])
        assert calls == 1, 'Loop must terminate after a single 0-row batch (already backfilled)'

    def test_loop_handles_partial_final_batch(self):
        calls = self._run_upgrade_with_mocked_execute([5000, 17, 0])
        assert calls == 3, (
            'A batch smaller than _BACKFILL_BATCH_SIZE must NOT be treated as terminal; '
            'only a 0-row batch ends the loop.'
        )


class TestBackfillVerificationLogging:
    """Hermes round-21 MED: pin the post-backfill verification's
    warn-on-mismatch behaviour as a CI gate.

    The verification query never fails the migration (the undercount is
    conservative and the column is operational), so without these tests
    a regression that silenced the warning would not be caught by CI.
    These tests close that gap by asserting the warning fires (and only
    fires) when ``mismatch_count > 0``.
    """

    def _run_upgrade_with_mismatch_count(self, mismatch_count: int) -> tuple[MagicMock, MagicMock]:
        m = _load_migration_033()
        # One backfill batch returning 0 rows — terminates the loop
        # immediately so the verification step is the only further DB
        # interaction.
        backfill_result = MagicMock()
        backfill_result.all.return_value = []

        verification_result = MagicMock()
        verification_result.scalar.return_value = mismatch_count

        bind = MagicMock()
        bind.execute.side_effect = [backfill_result, verification_result]

        mock_logger = MagicMock()

        with (
            patch.object(m.op, 'get_bind', return_value=bind),
            patch.object(m.op, 'add_column'),
            patch.object(m.op, 'create_index'),
            patch.object(m.op, 'create_check_constraint'),
            patch.object(m, '_column_exists', return_value=True),
            patch.object(m, '_constraint_exists', return_value=True),
            patch.object(m, '_index_exists', return_value=True),
            patch.object(m, 'logger', mock_logger),
        ):
            m.upgrade()

        return bind, mock_logger

    def test_warning_fires_when_mismatches_present(self):
        """Mismatch present → ``logger.warning`` is invoked exactly once."""
        _, mock_logger = self._run_upgrade_with_mismatch_count(7)
        mock_logger.warning.assert_called_once()
        # First positional arg is the format string; second is the count.
        args, _ = mock_logger.warning.call_args
        assert 'F22 backfill verification' in args[0]
        assert args[1] == 7

    def test_no_warning_when_no_mismatches(self):
        """``mismatch_count == 0`` → ``logger.warning`` is NOT invoked."""
        _, mock_logger = self._run_upgrade_with_mismatch_count(0)
        mock_logger.warning.assert_not_called()

    def test_verification_query_uses_count_aggregate(self):
        """Pin the verification SQL: a single ``COUNT(*)`` aggregate
        (Hermes round-20 LOW), not a per-unit list — so the deploy-log
        payload is bounded by construction regardless of mismatch
        cardinality."""
        source = _migration_033_source()
        # The verification block must select an aggregate count, not
        # a row list; matching the alias makes the assertion robust to
        # whitespace.
        assert 'SELECT COUNT(*) AS mismatched' in source, (
            'Verification SQL must aggregate to a single COUNT row.'
        )
