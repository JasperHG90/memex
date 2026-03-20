"""Add kv_entries table and title trigram index on notes.

Revision ID: 006_kv_and_title_trgm
Revises: 005_entity_meta_to_model
Create Date: 2026-03-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = '006_kv_and_title_trgm'
down_revision: Union[str, None] = '005_entity_meta_to_model'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    """Check if a table already exists (handles baseline create_all)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text('SELECT 1 FROM information_schema.tables WHERE table_name = :name'),
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
    # Enable pg_trgm extension for trigram indexes
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')

    # Create kv_entries table
    if not _table_exists('kv_entries'):
        op.create_table(
            'kv_entries',
            sa.Column(
                'id',
                sa.UUID(),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column(
                'vault_id',
                sa.UUID(),
                sa.ForeignKey('vaults.id', ondelete='CASCADE'),
                nullable=True,
                index=True,
            ),
            sa.Column('key', sa.Text(), nullable=False),
            sa.Column('value', sa.Text(), nullable=False),
            sa.Column('embedding', Vector(384), nullable=True),
            sa.Column(
                'created_at',
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                'updated_at',
                sa.TIMESTAMP(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint('vault_id', 'key', name='uq_kv_vault_key'),
            sa.Index(
                'idx_kv_global_key',
                'key',
                unique=True,
                postgresql_where=sa.text('vault_id IS NULL'),
            ),
        )

    # Add trigram GIN index on notes.title for fuzzy title search
    if not _index_exists('idx_notes_title_trgm'):
        op.execute(
            'CREATE INDEX idx_notes_title_trgm ON notes USING gin (lower(title) gin_trgm_ops)'
        )


def downgrade() -> None:
    op.drop_index('idx_notes_title_trgm', table_name='notes')
    op.drop_table('kv_entries')
