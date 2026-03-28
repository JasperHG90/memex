"""Add 'archived' to note lifecycle status.

Notes can now be marked as 'archived' to soft-delete them. Archived notes
have all their memory units marked as stale (excluded from retrieval).
This is used by the obsidian-sync tool when local files are deleted.

Revision ID: 012_note_archived_status
Revises: 011_remove_token_usage
Create Date: 2026-03-28
"""

from typing import Sequence, Union

from alembic import op

revision: str = '012_note_archived_status'
down_revision: Union[str, None] = '011_remove_token_usage'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old constraint and recreate with 'archived' included
    op.drop_constraint('ck_notes_status', 'notes', type_='check')
    op.create_check_constraint(
        'ck_notes_status',
        'notes',
        "status IN ('active', 'superseded', 'appended', 'archived')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_notes_status', 'notes', type_='check')
    op.create_check_constraint(
        'ck_notes_status',
        'notes',
        "status IN ('active', 'superseded', 'appended')",
    )
