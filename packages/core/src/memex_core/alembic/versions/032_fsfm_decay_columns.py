"""FSFM-lite decay scoring columns on memory_units.

Adds three nullable columns supporting the decay-boost composition and
FSFM pre-filter clause:

- ``importance: float`` — derived from ``intent_class``
  (permanent=1.0, durable=0.7, ephemeral=0.3); rows where ``intent_class``
  is somehow NULL stay ``importance = NULL`` (genuine missing
  classification, no synthetic default).
- ``stability: float`` — days; per-class table (permanent=NULL meaning
  infinity, durable=180, ephemeral=14).
- ``last_outcome_at: timestamptz`` — when the unit last had
  ``record_outcome`` called against it; explicitly left NULL on every
  pre-existing row so the NULL-handling guard treats them as
  "no temporal anchor -> neutral boost" rather than synthesising a
  decay-clock start point at backfill time.

The intent->importance and intent->stability values mirror the constants
in ``memex_core.memory.retrieval.constants`` so the SQL clause and Python
boost share one source of truth (the migration's CASE expressions
must stay in lockstep with that module).

Revision ID: 032_fsfm_decay_columns
Revises: 031_proposal_resolved_by
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '032_fsfm_decay_columns'
down_revision: Union[str, None] = '031_proposal_resolved_by'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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

    if not _column_exists(conn, 'memory_units', 'importance'):
        op.add_column(
            'memory_units',
            sa.Column('importance', sa.Float(), nullable=True),
        )

    if not _column_exists(conn, 'memory_units', 'stability'):
        op.add_column(
            'memory_units',
            sa.Column('stability', sa.Float(), nullable=True),
        )

    if not _column_exists(conn, 'memory_units', 'last_outcome_at'):
        op.add_column(
            'memory_units',
            sa.Column(
                'last_outcome_at',
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
        )

    op.execute(
        sa.text(
            'UPDATE memory_units SET importance = CASE intent_class '
            "  WHEN 'permanent' THEN 1.0 "
            "  WHEN 'durable' THEN 0.7 "
            "  WHEN 'ephemeral' THEN 0.3 "
            '  ELSE NULL '
            'END '
            'WHERE importance IS NULL'
        )
    )

    op.execute(
        sa.text(
            'UPDATE memory_units SET stability = CASE intent_class '
            "  WHEN 'durable' THEN 180.0 "
            "  WHEN 'ephemeral' THEN 14.0 "
            '  ELSE NULL '
            'END '
            'WHERE stability IS NULL '
            "AND intent_class IN ('durable', 'ephemeral')"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    if _column_exists(conn, 'memory_units', 'last_outcome_at'):
        op.drop_column('memory_units', 'last_outcome_at')

    if _column_exists(conn, 'memory_units', 'stability'):
        op.drop_column('memory_units', 'stability')

    if _column_exists(conn, 'memory_units', 'importance'):
        op.drop_column('memory_units', 'importance')
