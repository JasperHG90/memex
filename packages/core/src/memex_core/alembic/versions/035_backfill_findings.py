"""Backfill semantic_contradiction maintenance_proposals from existing contradicts links.

The contradiction engine started writing one ``semantic_contradiction``
``maintenance_proposals`` row per ``contradicts`` link in this branch.
Pre-existing links (created before this code was deployed) had no
corresponding finding, so ``memex lint findings`` only surfaces
contradictions detected after the deploy. This migration backfills
findings for every existing ``contradicts`` link, idempotent under
the partial unique index that gates per-(rule, target, vault) status='pending'.

The migration uses ``ON CONFLICT DO NOTHING`` against the existing partial
unique index, so re-running it after a partial backfill leaves the table
unchanged.

Revision ID: 035_backfill_findings
Revises: 034_add_mw_mode
Create Date: 2026-05-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '035_backfill_findings'
down_revision: str | None = '034_add_mw_mode'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BACKFILL_SQL = sa.text("""
    INSERT INTO maintenance_proposals (
        vault_id, lint_type, target_type, target_id,
        rule_name, evidence, suggested_action, status, source
    )
    SELECT
        ml.vault_id,
        'quality',
        'memory_unit',
        ml.to_unit_id::text,
        'semantic_contradiction',
        jsonb_build_object(
            'authoritative_unit_id',
                COALESCE(ml.link_metadata ->> 'authoritative_unit_id', ml.from_unit_id::text),
            'superseded_unit_id', ml.to_unit_id::text,
            'reasoning', LEFT(COALESCE(ml.link_metadata ->> 'reasoning', ''), 1000),
            'superseding_note_title',
                LEFT(COALESCE(ml.link_metadata ->> 'superseding_note_title', ''), 200),
            'backfilled', true
        ),
        'Review the contradiction and decide whether the superseded unit should be revised or removed.',
        'pending',
        'llm'
    FROM memory_links ml
    WHERE ml.link_type = 'contradicts'
    ON CONFLICT (rule_name, target_type, target_id, vault_id)
    WHERE status = 'pending'
    DO NOTHING
""")


def upgrade() -> None:
    op.get_bind().execute(_BACKFILL_SQL)


def downgrade() -> None:
    # The backfill is purely additive and shares the partial unique index
    # with run-time emissions, so identifying which rows came from the
    # backfill vs the engine is impossible without an extra column. Mark
    # this migration as one-way: downgrade is a no-op.
    pass
