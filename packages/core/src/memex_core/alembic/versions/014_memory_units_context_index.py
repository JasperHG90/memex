"""Add partial index on memory_units.context.

Speeds up context-filtered queries (e.g. source_context='user_notes')
by indexing only rows where context IS NOT NULL.

Revision ID: 014_memory_units_context_index
Revises: 013_vault_summaries
Create Date: 2026-04-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '014_memory_units_context_index'
down_revision: Union[str, None] = '013_vault_summaries'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 001_full_baseline uses SQLModel.metadata.create_all which may already
    # create this index if it's defined in the model's __table_args__.
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'ix_memory_units_context')"
        )
    )
    if result.scalar():
        return

    op.create_index(
        'ix_memory_units_context',
        'memory_units',
        ['context'],
        postgresql_where='context IS NOT NULL',
    )


def downgrade() -> None:
    op.drop_index('ix_memory_units_context', table_name='memory_units')
