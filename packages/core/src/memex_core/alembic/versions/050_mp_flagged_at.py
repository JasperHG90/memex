"""Add flagged_at column to maintenance_proposals.

Nullable timestamp column for the "flag for later" bookmark feature.
Orthogonal to resolution status — any finding can be flagged regardless
of its lifecycle state.

Revision ID: 050_mp_flagged_at
Revises: 049_lint_llm_signature
Create Date: 2026-05-26
"""

import sqlalchemy as sa
from alembic import op

revision: str = '050_mp_flagged_at'
down_revision: str | None = '049_lint_llm_signature'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.add_column(
        'maintenance_proposals',
        sa.Column('flagged_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('maintenance_proposals', 'flagged_at')
