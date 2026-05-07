"""Unit tests for migration 030_proposal_resolved_by (issue #34).

Static checks that don't need a database. The behavioural round-trip
assertion (column appears on upgrade, disappears on downgrade) lives in the
integration suite — exercised by ``test_int_f9_consolidate.py`` and
``test_int_f8_lint_query.py`` which both run a full ``alembic upgrade head``
through the testcontainer fixture.
"""

from __future__ import annotations

import importlib.util
import pathlib as plb
import re
from typing import Any


def _load_migration_031() -> Any:
    import memex_core

    package_dir = plb.Path(memex_core.__file__).resolve().parent
    migration_path = (
        package_dir / 'alembic' / 'versions' / '031_maintenance_proposals_resolved_by.py'
    )
    spec = importlib.util.spec_from_file_location('migration_031', migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_031_source() -> str:
    import memex_core

    package_dir = plb.Path(memex_core.__file__).resolve().parent
    migration_path = (
        package_dir / 'alembic' / 'versions' / '031_maintenance_proposals_resolved_by.py'
    )
    return migration_path.read_text(encoding='utf-8')


class TestMigration031Metadata:
    def test_revision_id_fits_in_alembic_version_column(self):
        m = _load_migration_031()
        # alembic_version.version_num is varchar(32) by default — keep ids short.
        assert len(m.revision) <= 32, (
            'revision id must fit in alembic_version.version_num (varchar(32))'
        )
        assert m.revision == '031_proposal_resolved_by'

    def test_down_revision_chains_from_030(self):
        m = _load_migration_031()
        assert m.down_revision == '030_revisit_last_reviewed_at'


class TestMigration031AddsNullableColumn:
    """The schema patch is purely additive: nullable resolved_by TEXT column."""

    def test_upgrade_adds_resolved_by_column(self):
        source = _migration_031_source()
        pattern = re.compile(
            r"add_column\(\s*'maintenance_proposals'\s*,\s*"
            r"sa\.Column\(\s*'resolved_by'\s*,\s*sa\.Text\(\)\s*,\s*nullable=True",
            re.DOTALL,
        )
        assert pattern.search(source), (
            "Expected add_column('maintenance_proposals', "
            "sa.Column('resolved_by', sa.Text(), nullable=True ...)) in upgrade()"
        )

    def test_downgrade_drops_resolved_by_column(self):
        source = _migration_031_source()
        pattern = re.compile(
            r"drop_column\(\s*'maintenance_proposals'\s*,\s*'resolved_by'\s*\)",
            re.DOTALL,
        )
        assert pattern.search(source), (
            "Expected drop_column('maintenance_proposals', 'resolved_by') in downgrade()"
        )


class TestMaintenanceProposalModelDefault:
    """Sanity check that the Python model marks the new column nullable."""

    def test_resolved_by_defaults_to_none(self):
        from memex_core.memory.sql_models import (
            LintSource,
            LintStatus,
            LintType,
            MaintenanceProposal,
        )

        proposal = MaintenanceProposal(
            lint_type=LintType.QUALITY,
            target_type='memory_unit',
            target_id='abc',
            rule_name='r',
            suggested_action='review',
            status=LintStatus.PENDING,
            source=LintSource.RULE,
        )
        assert proposal.resolved_by is None
