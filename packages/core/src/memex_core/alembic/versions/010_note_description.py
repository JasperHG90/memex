"""Add description column to notes table.

Revision ID: 010_note_description
Revises: 009_memory_units_search_tsvector
Create Date: 2026-03-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '010_note_description'
down_revision: Union[str, None] = '009_memory_units_search_tsvector'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check if a column already exists (handles baseline create_all)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table AND column_name = :col'
        ),
        {'table': table, 'col': column},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _column_exists('notes', 'description'):
        op.add_column(
            'notes',
            sa.Column('description', sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column('notes', 'description')
