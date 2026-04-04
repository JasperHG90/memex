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
    op.drop_column('vault_summaries', 'last_note_id')


def downgrade() -> None:
    op.add_column('vault_summaries', sa.Column('last_note_id', sa.Uuid(), nullable=True))
