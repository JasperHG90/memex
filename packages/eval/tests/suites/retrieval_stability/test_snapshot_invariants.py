"""Invariant tests for the shipped snapshot + Suite.shipped_snapshot_path.

These tests catch the gaps surfaced in the PR's adversarial review:

* The new ``Suite.shipped_snapshot_path`` field must round-trip through
  Pydantic validation and serialization without loss.
* The shipped snapshot's ``alembic_head`` must match the current
  alembic head; if a migration lands without ``refresh-snapshot``,
  this test fails with a clear pointer instead of letting CI break
  later in the suite-run path with an obscure import error.

Both tests run without a Memex server. Pure file + import checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Side-effect import: ensures the suite loads (decorators register).
import memex_eval.suites.retrieval_stability  # noqa: F401
from memex_eval.suite import load_suite


@pytest.fixture
def suite():
    return load_suite('retrieval_stability')


class TestShippedSnapshotPath:
    """``Suite.shipped_snapshot_path`` field survives the framework round-trip."""

    def test_field_is_populated_for_retrieval_stability(self, suite) -> None:
        assert suite.shipped_snapshot_path is not None
        assert suite.shipped_snapshot_path.is_dir(), (
            f'shipped snapshot dir missing at {suite.shipped_snapshot_path}. '
            'Run `memex-eval suite refresh-snapshot retrieval_stability` to regenerate.'
        )

    def test_pydantic_round_trip_preserves_path(self, suite) -> None:
        """``model_dump`` + ``model_validate`` must preserve the field.

        Snapshot serialization and any framework re-validation step
        must NOT silently drop the path.
        """
        dumped = suite.model_dump()
        assert 'shipped_snapshot_path' in dumped
        from memex_eval.suite.base import Suite as _LegacySuite

        re_validated = _LegacySuite.model_validate(dumped)
        assert re_validated.shipped_snapshot_path == suite.shipped_snapshot_path

    def test_field_is_optional_for_other_suites(self) -> None:
        """Suites that don't ship a snapshot must still load."""
        # acme_corp does not declare shipped_snapshot_path.
        other = load_suite('acme_corp')
        assert other.shipped_snapshot_path is None


class TestShippedSnapshotManifest:
    """The shipped snapshot's manifest must match the live alembic head.

    If a migration lands without refreshing the snapshot, the runner
    fails with ``Alembic head mismatch`` at import time — but only
    after wasting the entire suite-run setup. This pre-flight check
    surfaces the mismatch as a fast unit-test failure with a clear
    remediation hint.
    """

    def test_manifest_alembic_head_matches_live_head(self, suite) -> None:
        manifest_path = suite.shipped_snapshot_path / 'vaults' / '_default' / 'manifest.json'
        if not manifest_path.is_file():
            pytest.skip('snapshot not present locally — covered by refresh-snapshot workflow')
        manifest = json.loads(manifest_path.read_text())
        snapshot_head = manifest.get('alembic_head')
        assert snapshot_head, 'manifest missing alembic_head field'

        # Resolve the current alembic head from the migrations directory.
        # Importing memex_core's alembic config is the source of truth.
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        import memex_core

        ini = Path(memex_core.__file__).parent / 'alembic.ini'
        if not ini.is_file():
            pytest.skip(
                f'alembic.ini not found at {ini}; cannot validate snapshot freshness in this env'
            )
        cfg = Config(str(ini))
        script = ScriptDirectory.from_config(cfg)
        live_head = script.get_current_head()

        assert snapshot_head == live_head, (
            f'Shipped snapshot was captured at alembic head {snapshot_head!r} '
            f'but the live migration head is {live_head!r}. Run '
            '`memex-eval suite refresh-snapshot retrieval_stability` to regenerate '
            'the snapshot against the current schema.'
        )

    def test_manifest_pins_embedder_identity(self, suite) -> None:
        """The manifest must record the embedder used for capture.

        A silent embedder swap would invalidate every stored embedding
        but be invisible without this pin.
        """
        manifest_path = suite.shipped_snapshot_path / 'vaults' / '_default' / 'manifest.json'
        if not manifest_path.is_file():
            pytest.skip('snapshot not present locally')
        manifest = json.loads(manifest_path.read_text())
        emb = manifest.get('embedding_model') or {}
        assert emb.get('name'), 'manifest.embedding_model.name missing'
        assert emb.get('dim'), 'manifest.embedding_model.dim missing'
