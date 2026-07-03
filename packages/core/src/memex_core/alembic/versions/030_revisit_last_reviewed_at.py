"""revisit_last_reviewed_at — record last-review timestamp for FSRS-5.

Adds one column to ``memory_units``:
- ``revisit_last_reviewed_at`` (TIMESTAMPTZ, NULL): wall-clock timestamp of the
  most recent review (when it was last *reviewed*), distinct from ``revisit_due_at``
  (when it is *next due*).

The previous shape conflated the two — passing ``revisit_due_at`` into
``UnitState.last_review`` made FSRS-5 compute ``elapsed_days = (now - next_due)``
instead of ``(now - last_reviewed)``, collapsing intervals to ~0 days for
on-time reviews. This column carves the two concerns apart so the FSRS-5
elapsed-days computation receives the correct input.

Nullable because legacy rows from migration 026 have no recorded
last-review timestamp; ``services.revisitation.review`` falls back to
``revisit_due_at`` when this column is NULL on a unit that already has FSRS
state populated.

Revision ID: 030_revisit_last_reviewed_at
Revises: 029_lint_llm_quota
Create Date: 2026-05-01
"""

import sqlalchemy as sa
from alembic import op

revision: str = '030_revisit_last_reviewed_at'
down_revision: str | None = '029_lint_llm_quota'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


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

    if not _column_exists(conn, 'memory_units', 'revisit_last_reviewed_at'):
        op.add_column(
            'memory_units',
            sa.Column('revisit_last_reviewed_at', sa.TIMESTAMP(timezone=True), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _column_exists(conn, 'memory_units', 'revisit_last_reviewed_at'):
        op.drop_column('memory_units', 'revisit_last_reviewed_at')
