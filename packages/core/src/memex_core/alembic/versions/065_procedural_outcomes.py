"""Phase 2 outcome counters on ``procedural_entries`` (§18.5).

Adds the success/failure/mixed/uses counters + ``last_used_at`` that the
ranking surface (Beta-Bernoulli posterior over success/failure) and
briefing curation consume. Counters live ON the entry — not a side table
keyed on KV strings (028's structural fragility, now avoidable). All
default 0 / NULL, so the column add is inert for existing rows.

Revision ID: 065_procedural_outcomes
Revises: 064_two_kind_plane
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '065_procedural_outcomes'
down_revision: str | None = '064_two_kind_plane'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INT_COLS = ('success_count', 'failure_count', 'mixed_count', 'uses')


def upgrade() -> None:
    for col in _INT_COLS:
        op.add_column(
            'procedural_entries',
            sa.Column(col, sa.Integer(), nullable=False, server_default='0'),
        )
    op.add_column(
        'procedural_entries',
        sa.Column('last_used_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('procedural_entries', 'last_used_at')
    for col in reversed(_INT_COLS):
        op.drop_column('procedural_entries', col)
