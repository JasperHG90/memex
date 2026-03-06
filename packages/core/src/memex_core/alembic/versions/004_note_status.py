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


def upgrade() -> None:
    op.add_column(
        'notes',
        sa.Column('status', sa.Text(), nullable=False, server_default='active'),
    )
    op.add_column(
        'notes',
        sa.Column('superseded_by', sa.UUID(), nullable=True),
    )
    op.add_column(
        'notes',
        sa.Column('appended_to', sa.UUID(), nullable=True),
    )
    op.create_index('ix_notes_status', 'notes', ['status'])
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
