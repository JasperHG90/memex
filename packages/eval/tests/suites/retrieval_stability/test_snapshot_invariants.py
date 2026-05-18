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
        # Stronger than is_dir() — an empty directory passes is_dir()
        # but produces an obscure import error at runner setup time.
        # Assert the marker AND the manifest so an empty-shell snapshot
        # fails this test, not the suite-run path.
        marker = suite.shipped_snapshot_path / '_complete.marker'
        manifest = suite.shipped_snapshot_path / 'vaults' / '_default' / 'manifest.json'
        assert marker.is_file(), (
            f'shipped snapshot at {suite.shipped_snapshot_path} is missing '
            f'the _complete.marker sentinel. The snapshot is incomplete; '
            'rerun refresh-snapshot.'
        )
        assert manifest.is_file(), (
            f'shipped snapshot at {suite.shipped_snapshot_path} is missing '
            f'vaults/_default/manifest.json. Rerun refresh-snapshot.'
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

    def test_decorator_build_preserves_path(self, tmp_path) -> None:
        """The decorator's ``Suite.build()`` (the actual production path
        from suite ``__init__.py`` → legacy Suite) must thread
        ``shipped_snapshot_path`` through to the legacy model.

        The Pydantic ``model_validate`` test above only exercises the
        deserialize path; the runner consumes the object built by
        ``decorator.Suite.build()`` directly. Cover both.
        """
        from memex_eval.suite import SuiteMetadata, SuiteSources
        from memex_eval.suite.decorator import Suite as _DecoratorSuite

        metadata = SuiteMetadata(
            name='_snapshot_path_round_trip_test',
            schema_version='1',
            suite_version='1.0.0',
            description='d',
            tags=['test'],
            primary_metrics=['suite.pass_rate'],
            components_under_test=[],
            knobs=[],
        )
        snap_dir = tmp_path / 'snapshot'
        snap_dir.mkdir()
        decorator_suite = _DecoratorSuite(
            metadata=metadata,
            sources=SuiteSources(notes=[]),
            shipped_snapshot_path=snap_dir,
        )
        legacy = decorator_suite.build()
        assert legacy.shipped_snapshot_path == snap_dir

    def test_field_is_optional_for_other_suites(self) -> None:
        """Suites that don't ship a snapshot must still load."""
        # acme_corp does not declare shipped_snapshot_path.
        other = load_suite('acme_corp')
        assert other.shipped_snapshot_path is None


class TestSourceSuiteQueryFloor:
    """Guard against silent-omission regressions in source-suite query enumeration.

    Each source corpus exposes its scenarios as pure data via
    ``scenarios.py`` (see ``memex_eval.suite.read_scenario_specs``).
    If a future refactor in any of the upstream corpora drops or
    renames queries — or removes the ``scenarios.py`` data file
    entirely — the corresponding scenarios silently vanish from this
    suite. A hard-floor count test catches the regression at CI time
    rather than at next-capture time. The floor values reflect the
    corpus state at PR-merge time; bump on intentional additions,
    decrease only after explicit operator review of what was lost.
    """

    _EXPECTED_QUERY_FLOOR: dict[str, int] = {
        'acme_corp': 36,
        'ai_research_lab': 7,
        'project_nexus': 8,
    }

    @pytest.mark.parametrize(
        'corpus,floor',
        sorted(_EXPECTED_QUERY_FLOOR.items()),
    )
    def test_source_suite_query_count_meets_floor(self, corpus: str, floor: int) -> None:
        from memex_eval.suites.retrieval_stability import _queries_from_source_suite

        queries = _queries_from_source_suite(corpus)
        assert len(queries) >= floor, (
            f'retrieval_stability enumerated {len(queries)} queries from '
            f'corpus {corpus!r}, below the floor of {floor}. Either a '
            f'source suite removed or renamed scenarios in scenarios.py, '
            f'or queries were intentionally removed. Investigate before '
            f'lowering the floor in this test.'
        )


class TestReadScenarioSpecsIsSideEffectFree:
    """``read_scenario_specs(name)`` MUST NOT trigger the source suite's
    full registration. This is the load-bearing architectural contract
    that lets ``retrieval_stability`` enumerate source-corpus queries
    cheaply (no Suite construction, no setup_action registration, no
    eager memex_core import).

    The pre-Option-A approach (AST scraping) achieved this by avoiding
    Python imports entirely. The post-Option-A approach achieves it via
    ``spec_from_file_location`` loading only ``scenarios.py``. Both
    paths guard against the source suite's ``__init__.py`` firing.

    Failure mode this catches: someone moves a ``suite.register(...)``
    call into ``scenarios.py`` (data + registration coupling),
    re-introducing the side effect.
    """

    @pytest.mark.parametrize('corpus', ['acme_corp', 'ai_research_lab', 'project_nexus'])
    def test_read_does_not_import_parent_package(self, corpus: str) -> None:
        import sys

        from memex_eval.suite import read_scenario_specs

        module_path = f'memex_eval.suites.{corpus}'
        # If a previous test imported the parent, drop it so we test
        # the contract clean.
        sys.modules.pop(module_path, None)
        sys.modules.pop(f'{module_path}.scenarios', None)
        sys.modules.pop(f'_scenarios_only.{corpus}.scenarios', None)

        specs = read_scenario_specs(corpus)
        assert specs, f'{corpus} returned no scenario specs'
        # Parent package must NOT be in sys.modules — that would mean
        # __init__.py ran, defeating the whole point.
        assert module_path not in sys.modules, (
            f'read_scenario_specs({corpus!r}) imported the parent package; '
            f"that re-triggers __init__.py's full registration and breaks "
            f'the side-effect-free contract.'
        )


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
        # Manifest absence is now a hard fail (covered by
        # test_field_is_populated_for_retrieval_stability above),
        # not a skip — a missing manifest means the snapshot is broken
        # and should not pass tests silently.
        assert manifest_path.is_file(), (
            f'snapshot manifest missing at {manifest_path}. Run '
            '`memex-eval suite refresh-snapshot retrieval_stability` to regenerate.'
        )
        manifest = json.loads(manifest_path.read_text())
        snapshot_head = manifest.get('alembic_head')
        assert snapshot_head, 'manifest missing alembic_head field'

        # Resolve the current alembic head from the migrations directory.
        # Importing memex_core's alembic config is the source of truth.
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        import memex_core

        ini = Path(memex_core.__file__).parent / 'alembic.ini'
        # Missing alembic.ini is a packaging regression — fail hard so
        # the snapshot can't ship without an alembic-head guard. (The
        # previous version of this test skipped on missing ini, letting
        # the gate go silently green on misconfigured environments.)
        assert ini.is_file(), (
            f'alembic.ini missing at {ini}; cannot validate snapshot '
            'freshness. This is a packaging regression in memex_core.'
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
        assert manifest_path.is_file(), f'snapshot manifest missing at {manifest_path}.'
        manifest = json.loads(manifest_path.read_text())
        emb = manifest.get('embedding_model') or {}
        assert emb.get('name'), 'manifest.embedding_model.name missing'
        assert emb.get('dim'), 'manifest.embedding_model.dim missing'
