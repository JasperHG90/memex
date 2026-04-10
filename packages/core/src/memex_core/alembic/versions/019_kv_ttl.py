"""Add expires_at column to kv_entries for TTL support.

Nullable TIMESTAMP WITH TIME ZONE column. NULL means the entry never
expires.  A partial btree index accelerates both the read-time filter
and the periodic cleanup DELETE.

Revision ID: 019_kv_ttl
Revises: 018_vault_summary_needs_regen
Create Date: 2026-04-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '019_kv_ttl'
down_revision: Union[str, None] = '018_vault_summary_needs_regen'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table AND column_name = :column)'
        ),
        {'table': table, 'column': column},
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
    if not _column_exists(conn, 'kv_entries', 'expires_at'):
        op.add_column(
            'kv_entries',
            sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=True),
        )
    if not _index_exists(conn, 'idx_kv_expires_at'):
        op.create_index(
            'idx_kv_expires_at',
            'kv_entries',
            ['expires_at'],
            postgresql_using='btree',
            postgresql_where=sa.text('expires_at IS NOT NULL'),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _index_exists(conn, 'idx_kv_expires_at'):
        op.drop_index('idx_kv_expires_at', table_name='kv_entries')
    if _column_exists(conn, 'kv_entries', 'expires_at'):
        op.drop_column('kv_entries', 'expires_at')
