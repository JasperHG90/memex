"""Add summary_version_incorporated to notes.

Tracks which vault summary version last incorporated each note.
NULL means "not yet incorporated" — the note will be picked up on the
next update or regeneration cycle.

Includes a backfill: active notes in vaults that already have a summary
are marked with that summary's current version so they don't trigger a
redundant full reprocess on the first scheduler tick after deploy.

Revision ID: 016_note_summary_version
Revises: 015_drop_vs_last_note_id
Create Date: 2026-04-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '016_note_summary_version'
down_revision: Union[str, None] = '015_drop_vs_last_note_id'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT 1 FROM information_schema.columns '
            "WHERE table_name = 'notes' AND column_name = 'summary_version_incorporated'"
        )
    )
    if not result.fetchone():
        op.add_column(
            'notes',
            sa.Column('summary_version_incorporated', sa.Integer(), nullable=True),
        )
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = 'idx_notes_summary_version'")
    )
    if not result.fetchone():
        op.create_index(
            'idx_notes_summary_version',
            'notes',
            ['vault_id', 'summary_version_incorporated'],
        )

    # Backfill: mark active notes as incorporated at their vault's current
    # summary version.  Notes in vaults without a summary stay NULL (correct).
    op.execute(
        sa.text(
            'UPDATE notes SET summary_version_incorporated = vs.version '
            'FROM vault_summaries vs '
            "WHERE notes.vault_id = vs.vault_id AND notes.status = 'active'"
        )
    )


def downgrade() -> None:
    op.drop_index('idx_notes_summary_version', table_name='notes')
    op.drop_column('notes', 'summary_version_incorporated')
