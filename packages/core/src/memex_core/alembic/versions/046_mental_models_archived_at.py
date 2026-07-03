"""Add archived_at column to mental_models.

Supports the ``archive_mental_model`` maintenance-proposal action. An archived
model is hidden from retrieval, survey, reflection, and HTTP listings; the row
itself stays so the action remains reversible (reverse clears the column).
Read-paths gain a ``WHERE archived_at IS NULL`` filter alongside this column.

Revision ID: 046_mental_models_archived_at
Revises: 045_drop_procedure_outcomes
Create Date: 2026-05-23
"""

import sqlalchemy as sa
from alembic import op

revision: str = '046_mental_models_archived_at'
down_revision: str | None = '045_drop_procedure_outcomes'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.add_column(
        'mental_models',
        sa.Column('archived_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        'idx_mental_models_archived_at',
        'mental_models',
        ['archived_at'],
        unique=False,
        postgresql_where=sa.text('archived_at IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('idx_mental_models_archived_at', table_name='mental_models')
    op.drop_column('mental_models', 'archived_at')
