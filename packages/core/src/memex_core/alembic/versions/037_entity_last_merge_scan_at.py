"""entity_last_merge_scan_at — record cooldown timestamp for cross-batch entity-merge scans.

Adds one nullable column to ``entities``:

- ``last_merge_scan_at`` (TIMESTAMPTZ, NULL): wall-clock timestamp of the
  most recent inclusion in an entity-cluster collapse scan. NULL = never
  scanned (eligible on the first pass).

Also creates a partial index ``idx_entities_last_merge_scan_at`` over
non-NULL rows so the cooldown filter ``WHERE last_merge_scan_at IS NULL OR
last_merge_scan_at < now() - interval`` can range-scan the timestamped rows
without touching the (much larger) never-scanned set.

Additionally creates a partial expression index
``idx_maintenance_proposals_collapse_composition`` over
``evidence ->> 'composition_hash'`` for pending ``entity_collapse_cluster``
proposals. The rescan-collision lookup keys by composition_hash (cluster
membership fingerprint) so a winner shift between scans does not split into
duplicate findings.

Backfill: NULL on every existing entity — first scan post-deploy treats
the entire population as eligible.

The ``Entity`` table is global (no ``vault_id`` column). ``last_merge_scan_at``
is therefore a single per-entity cooldown timestamp.

Revision ID: 037_entity_last_merge_scan_at
Revises: 036_fsfm_cooldown_index
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op


revision: str = '037_entity_last_merge_scan_at'
down_revision: str | None = '036_fsfm_cooldown_index'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


_INDEX_NAME = 'idx_entities_last_merge_scan_at'
_COMPOSITION_INDEX_NAME = 'idx_maintenance_proposals_collapse_composition'


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.columns'
            '  WHERE table_schema = current_schema()'
            '    AND table_name = :table AND column_name = :column'
            ')'
        ),
        {'table': table, 'column': column},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, 'entities', 'last_merge_scan_at'):
        op.add_column(
            'entities',
            sa.Column('last_merge_scan_at', sa.TIMESTAMP(timezone=True), nullable=True),
        )

    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_INDEX_NAME}
        ON entities (last_merge_scan_at)
        WHERE last_merge_scan_at IS NOT NULL
        """
    )

    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_COMPOSITION_INDEX_NAME}
        ON maintenance_proposals ((evidence ->> 'composition_hash'))
        WHERE status = 'pending' AND rule_name = 'entity_collapse_cluster'
        """
    )


def downgrade() -> None:
    op.execute(f'DROP INDEX IF EXISTS {_COMPOSITION_INDEX_NAME}')
    op.execute(f'DROP INDEX IF EXISTS {_INDEX_NAME}')

    conn = op.get_bind()
    if _column_exists(conn, 'entities', 'last_merge_scan_at'):
        op.drop_column('entities', 'last_merge_scan_at')
