"""Add note lifecycle status columns.

Revision ID: 004_note_status
Revises: 003_contradiction_detection
Create Date: 2026-03-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '004_note_status'
down_revision: Union[str, None] = '003_contradiction_detection'
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


def _constraint_exists(name: str) -> bool:
    """Check if a constraint already exists."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text('SELECT 1 FROM pg_constraint WHERE conname = :name'),
        {'name': name},
    )
    return result.scalar() is not None


def _index_exists(name: str) -> bool:
    """Check if an index already exists."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text('SELECT 1 FROM pg_indexes WHERE indexname = :name'),
        {'name': name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _column_exists('notes', 'status'):
        op.add_column(
            'notes',
            sa.Column('status', sa.Text(), nullable=False, server_default='active'),
        )
    if not _column_exists('notes', 'superseded_by'):
        op.add_column(
            'notes',
            sa.Column('superseded_by', sa.UUID(), nullable=True),
        )
    if not _column_exists('notes', 'appended_to'):
        op.add_column(
            'notes',
            sa.Column('appended_to', sa.UUID(), nullable=True),
        )
    if not _index_exists('ix_notes_status'):
        op.create_index('ix_notes_status', 'notes', ['status'])
    if not _constraint_exists('ck_notes_status'):
        op.create_check_constraint(
            'ck_notes_status',
            'notes',
            "status IN ('active', 'superseded', 'appended')",
        )


def downgrade() -> None:
    op.drop_constraint('ck_notes_status', 'notes', type_='check')
    op.drop_index('ix_notes_status', table_name='notes')
    op.drop_column('notes', 'appended_to')
    op.drop_column('notes', 'superseded_by')
    op.drop_column('notes', 'status')
