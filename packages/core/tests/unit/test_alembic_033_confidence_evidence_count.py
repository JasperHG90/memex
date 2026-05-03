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
        results = [MagicMock(rowcount=rc) for rc in rowcounts]

        bind = MagicMock()
        bind.execute.side_effect = results

        ctx = MagicMock()
        ctx.autocommit_block.return_value.__enter__ = MagicMock(return_value=None)
        ctx.autocommit_block.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(m.op, 'get_bind', return_value=bind),
            patch.object(m.op, 'get_context', return_value=ctx),
            patch.object(m.op, 'add_column'),
            patch.object(m.op, 'create_index'),
            patch.object(m.op, 'create_check_constraint'),
            patch.object(m, '_column_exists', return_value=True),
            patch.object(m, '_constraint_exists', return_value=True),
            patch.object(m, '_index_exists', return_value=True),
        ):
            m.upgrade()

        return bind.execute.call_count

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
