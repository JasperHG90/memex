"""F20 revisit columns + partial index — adds FSRS-5 schedule state to memory_units.

Adds four columns to `memory_units`:
- revisit_due_at (TIMESTAMPTZ, NULL): when the unit is next due for review
- revisit_stability (FLOAT, NULL): FSRS-5 stability state
- revisit_difficulty (FLOAT, NULL): FSRS-5 difficulty state — required because
  py-fsrs evolves difficulty per review independently of MW counters; we cannot
  recompute it from (success_co_count, failure_co_count)
- revisit_review_count (INT, NOT NULL DEFAULT 0): consecutive Again-rating
  count for the sticky auto-deprioritize gate (resets to 0 on Hard/Good/Easy)

Plus a partial index `idx_memory_units_revisit_due_at WHERE revisit_due_at IS NOT NULL`
so the daily-tick `WHERE revisit_due_at <= now()` query stays fast on large vaults.

The first three columns are nullable because ineligible units (intent_class='ephemeral',
is_deprioritized=true, status='stale', etc.) never get scheduled. The partial index keeps
the indexed-row count proportional to *eligible-and-scheduled* units only.

`revisit_review_count` is NOT NULL with a server default of 0 so the streak counter is
always defined — every unit starts at zero consecutive Again ratings, regardless of
whether it has been scheduled.

FSRS-5 algorithm note: py-fsrs 4.1.2 implements FSRS-5 (19 weights), not FSRS-4.5 —
verified at .dev-team-artifacts/dev-tier-a-cognitive-memory/pocs/003-f20-fsrs-parity/
paper-cross-check.md. The schema is algorithm-version-agnostic; only column SEMANTICS
(stability + difficulty + due_at + streak count) are load-bearing.

Revision ID: 026_revisit_columns
Revises: 025_maintenance_proposals
Create Date: 2026-04-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '026_revisit_columns'
down_revision: Union[str, None] = '025_maintenance_proposals'
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


def _index_exists(conn, index: str) -> bool:
    result = conn.execute(
        sa.text('SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :index)'),
        {'index': index},
    )
    return bool(result.scalar())


def upgrade() -> None:
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
                server_default=sa.text('0'),
                nullable=False,
            ),
        )

    if not _index_exists(conn, 'idx_memory_units_revisit_due_at'):
        op.create_index(
            'idx_memory_units_revisit_due_at',
            'memory_units',
            ['revisit_due_at'],
            postgresql_where=sa.text('revisit_due_at IS NOT NULL'),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_memory_units_revisit_due_at'):
        op.drop_index('idx_memory_units_revisit_due_at', table_name='memory_units')

    for col_name in (
        'revisit_review_count',
        'revisit_difficulty',
        'revisit_stability',
        'revisit_due_at',
    ):
        if _column_exists(conn, 'memory_units', col_name):
            op.drop_column('memory_units', col_name)
