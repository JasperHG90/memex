"""Add note_appends audit table for atomic note-append idempotency.

Stores one row per successful call to POST /api/v1/notes/append, keyed on the
caller-supplied append_id (UUID). Retries with the same append_id replay the
cached outcome from this table instead of re-mutating the body.

Revision ID: 022_note_appends
Revises: 021_batch_jobs_input_note_keys
Create Date: 2026-04-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = '022_note_appends'
down_revision: Union[str, None] = '021_batch_jobs_input_note_keys'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, table: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = :table)'
        ),
        {'table': table},
    )
    return bool(result.scalar())


def _index_exists(conn, index: str) -> bool:
    result = conn.execute(
        sa.text('SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :index)'),
        {'index': index},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, 'note_appends'):
        op.create_table(
            'note_appends',
            sa.Column('append_id', UUID(as_uuid=True), primary_key=True),
            sa.Column('note_id', UUID(as_uuid=True), nullable=False),
            sa.Column('delta_sha256', sa.Text(), nullable=False),
            sa.Column('delta_bytes', sa.Integer(), nullable=False),
            sa.Column('joiner', sa.Text(), nullable=False),
            sa.Column('resulting_content_hash', sa.Text(), nullable=False),
            sa.Column(
                'new_unit_ids',
                ARRAY(UUID(as_uuid=True)),
                nullable=False,
                server_default=sa.text('ARRAY[]::uuid[]'),
            ),
            sa.Column(
                'applied_at',
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ['note_id'],
                ['notes.id'],
                name='note_appends_note_fkey',
                ondelete='CASCADE',
            ),
        )

    if not _index_exists(conn, 'idx_note_appends_note_id_applied_at'):
        op.create_index(
            'idx_note_appends_note_id_applied_at',
            'note_appends',
            ['note_id', 'applied_at'],
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_note_appends_note_id_applied_at'):
        op.drop_index('idx_note_appends_note_id_applied_at', table_name='note_appends')

    if _table_exists(conn, 'note_appends'):
        op.drop_table('note_appends')
