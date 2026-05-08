"""Drop FSRS-5 revisit columns from memory_units.

Reverses the additions from migrations 026 (``revisit_due_at``,
``revisit_stability``, ``revisit_difficulty``, ``revisit_review_count``
plus partial index ``idx_memory_units_revisit_due_at``) and 030
(``revisit_last_reviewed_at``) as part of removing the FSRS-5
revisit/review subsystem.

The unrelated decay columns added by migration 032
(``importance``, ``stability``, ``last_outcome_at``) are NOT touched —
those serve the FSFM-decay scoring path, which the new graph-aware
deprioritization scorer continues to consume.

Downgrade restores the columns and partial index for rollback parity
(types and defaults match the original 026/030 definitions).

Operational note (lock window): ``ALTER TABLE ... DROP COLUMN`` on
``memory_units`` takes an ACCESS EXCLUSIVE lock on the table for the
duration of the catalog flip — Postgres only marks the column as
dropped, it does NOT rewrite the heap, so the actual lock window is
short (milliseconds) on any normally-loaded server. The five
``revisit_*`` columns are dropped sequentially in one transaction, so
the cumulative lock window is still short. No data is migrated.

Revision ID: 035_drop_fsrs_revisit_columns
Revises: 034_add_mw_mode
Create Date: 2026-05-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '035_drop_fsrs_revisit_columns'
down_revision: Union[str, None] = '034_add_mw_mode'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PARTIAL_INDEX_NAME = 'idx_memory_units_revisit_due_at'


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


def _index_exists(conn, index_name: str) -> bool:
    result = conn.execute(
        sa.text('SELECT 1 FROM pg_indexes WHERE indexname = :ix'),
        {'ix': index_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, _PARTIAL_INDEX_NAME):
        op.drop_index(_PARTIAL_INDEX_NAME, table_name='memory_units')

    for column in (
        'revisit_last_reviewed_at',
        'revisit_review_count',
        'revisit_difficulty',
        'revisit_stability',
        'revisit_due_at',
    ):
        if _column_exists(conn, 'memory_units', column):
            op.drop_column('memory_units', column)


def downgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, 'memory_units', 'revisit_due_at'):
        op.add_column(
            'memory_units',
            sa.Column('revisit_due_at', sa.TIMESTAMP(timezone=True), nullable=True),
        )

    if not _column_exists(conn, 'memory_units', 'revisit_stability'):
        op.add_column(
            'memory_units',
            sa.Column('revisit_stability', sa.Float(), nullable=True),
        )

    if not _column_exists(conn, 'memory_units', 'revisit_difficulty'):
        op.add_column(
            'memory_units',
            sa.Column('revisit_difficulty', sa.Float(), nullable=True),
        )

    if not _column_exists(conn, 'memory_units', 'revisit_review_count'):
        op.add_column(
            'memory_units',
            sa.Column(
                'revisit_review_count',
                sa.Integer(),
                nullable=False,
                server_default='0',
            ),
        )

    if not _column_exists(conn, 'memory_units', 'revisit_last_reviewed_at'):
        op.add_column(
            'memory_units',
            sa.Column(
                'revisit_last_reviewed_at',
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
        )

    if not _index_exists(conn, _PARTIAL_INDEX_NAME):
        op.create_index(
            _PARTIAL_INDEX_NAME,
            'memory_units',
            ['revisit_due_at'],
            postgresql_where=sa.text('revisit_due_at IS NOT NULL'),
        )
