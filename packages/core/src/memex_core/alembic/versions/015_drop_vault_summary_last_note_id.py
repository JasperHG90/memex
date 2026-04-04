"""Drop orphaned last_note_id column from vault_summaries.

The column was part of the original per-note patching design but is
never populated after the switch to time-based updates.

Revision ID: 015_drop_vs_last_note_id
Revises: 014_memory_units_context_index
Create Date: 2026-04-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '015_drop_vs_last_note_id'
down_revision: Union[str, None] = '014_memory_units_context_index'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 001_full_baseline uses SQLModel.metadata.create_all which creates
    # the table from the current model (without last_note_id). Only drop
    # the column if it actually exists.
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
            "WHERE table_name = 'vault_summaries' AND column_name = 'last_note_id')"
        )
    )
    if not result.scalar():
        return

    op.drop_column('vault_summaries', 'last_note_id')


def downgrade() -> None:
    op.add_column('vault_summaries', sa.Column('last_note_id', sa.Uuid(), nullable=True))
