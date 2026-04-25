"""Unit tests for Alembic migration 021_batch_jobs_input_note_keys.

The behavioural assertions about the column/index landing on a real Postgres
schema live in `tests/integration/test_int_alembic_021.py`. The tests here
exercise the parts that don't need a database connection:

- The literal default `'[]'::jsonb` is hard-coded in the migration source
  (no server-side function call → on PG ≥ 11 the ALTER is metadata-only).
- The PG ≥ 11 version-guard helper raises on simulated PG 10 connections and
  accepts simulated PG 11+ connections.
"""

from __future__ import annotations

import importlib.util
import pathlib as plb
import re
from typing import Any
from unittest.mock import MagicMock

import pytest


def _load_migration_021() -> Any:
    """Load 021_batch_jobs_input_note_keys.py as a module.

    The migration file lives in `memex_core/alembic/versions/`. We load it via
    `importlib` rather than imports because Alembic versions aren't on
    `sys.path` and the filename starts with a digit.
    """
    import memex_core

    package_dir = plb.Path(memex_core.__file__).resolve().parent
    migration_path = package_dir / 'alembic' / 'versions' / '021_batch_jobs_input_note_keys.py'
    spec = importlib.util.spec_from_file_location('migration_021', migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_021_source() -> str:
    import memex_core

    package_dir = plb.Path(memex_core.__file__).resolve().parent
    migration_path = package_dir / 'alembic' / 'versions' / '021_batch_jobs_input_note_keys.py'
    return migration_path.read_text(encoding='utf-8')


class TestMigration021Metadata:
    """Static checks on the migration module's revision identifiers."""

    def test_revision_id(self):
        m = _load_migration_021()
        assert m.revision == '021_batch_jobs_input_note_keys'

    def test_down_revision_chains_from_020(self):
        m = _load_migration_021()
        assert m.down_revision == '020_temporal_cooccurrences'


class TestMigration021UsesLiteralDefault:
    """AC-019 (c): verify the migration uses a literal `'[]'::jsonb` default,
    not a function call. PG ≥ 11 stores literal defaults in pg_attribute and
    skips the table rewrite — but only for non-volatile constants.
    """

    def test_021_migration_uses_literal_default(self):
        source = _migration_021_source()
        # The literal default must appear inside a sa.text(...) call attached to
        # `server_default=`. Match `server_default=...sa.text("'[]'::jsonb")` with
        # tolerance for whitespace.
        pattern = re.compile(
            r"server_default\s*=\s*sa\.text\(\s*\"'\[\]'::jsonb\"\s*\)",
            re.DOTALL,
        )
        assert pattern.search(source), (
            'Expected `server_default=sa.text("\'[]\'::jsonb")` in migration 021. '
            'PG ≥ 11 only treats *literal* defaults as metadata-only; a function '
            'call (e.g. `sa.func.jsonb_build_array()`) would force a table rewrite.'
        )

    def test_021_migration_does_not_use_function_default(self):
        source = _migration_021_source()
        # Belt-and-braces: explicitly forbid `server_default=sa.func.*` on the new column.
        forbidden = re.compile(r'server_default\s*=\s*sa\.func\.')
        assert not forbidden.search(source), (
            'Migration 021 must not use sa.func.* as a server_default for input_note_keys; '
            'a function call is volatile-by-default and forces a table rewrite on PG ≥ 11.'
        )


class TestMigration021PgVersionGuard:
    """AC-019 (d): the migration's `_assert_pg_version_at_least(11)` helper must
    raise on PG < 11 and accept PG ≥ 11. We stub `conn.execute(...).scalar()` so
    the test does not need a database.
    """

    def _stub_conn(self, version_num: int) -> MagicMock:
        """Build a MagicMock conn whose `execute(...)._scalar()` returns version_num."""
        conn = MagicMock()
        result = MagicMock()
        result.scalar.return_value = version_num
        conn.execute.return_value = result
        return conn

    def test_021_migration_rejects_pg_below_11(self):
        m = _load_migration_021()
        # PG 10.20 -> server_version_num == 100020
        conn = self._stub_conn(100020)
        with pytest.raises(RuntimeError, match='requires Postgres >= 11'):
            m._assert_pg_version_at_least(conn, 11)

    def test_021_migration_rejects_pg_below_11_at_boundary(self):
        m = _load_migration_021()
        # The boundary case: just below the threshold.
        conn = self._stub_conn(109999)
        with pytest.raises(RuntimeError, match='requires Postgres >= 11'):
            m._assert_pg_version_at_least(conn, 11)

    def test_021_migration_accepts_pg_11_and_later(self):
        m = _load_migration_021()
        for vn in (110000, 110001, 130000, 180000):
            conn = self._stub_conn(vn)
            # Must not raise.
            m._assert_pg_version_at_least(conn, 11)

    def test_021_migration_handles_missing_version_num(self):
        """If `SHOW server_version_num` returned NULL (impossible in practice but
        defensive), the helper coerces to 0 and rejects."""
        m = _load_migration_021()
        conn = MagicMock()
        result = MagicMock()
        result.scalar.return_value = None
        conn.execute.return_value = result
        with pytest.raises(RuntimeError, match='requires Postgres >= 11'):
            m._assert_pg_version_at_least(conn, 11)
