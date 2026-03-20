"""Replace vault-based KV scoping with namespace prefixes.

Migrates existing keys to use namespace prefixes (global:, user:, project:),
drops the vault_id column, and creates a simple UNIQUE(key) constraint.

Revision ID: 007_kv_namespace_prefix
Revises: 006_kv_and_title_trgm
Create Date: 2026-03-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '007_kv_namespace_prefix'
down_revision: Union[str, None] = '006_kv_and_title_trgm'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _constraint_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT 1 FROM information_schema.table_constraints '
            "WHERE constraint_name = :name AND table_name = 'kv_entries'"
        ),
        {'name': name},
    )
    return result.scalar() is not None


def _index_exists(name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text('SELECT 1 FROM pg_indexes WHERE indexname = :name'),
        {'name': name},
    )
    return result.scalar() is not None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table AND column_name = :column'
        ),
        {'table': table, 'column': column},
    )
    return result.scalar() is not None


def upgrade() -> None:
    conn = op.get_bind()

    # --- Step 1: Migrate key prefixes ---
    # agents: -> project:
    conn.execute(
        sa.text(
            "UPDATE kv_entries SET key = 'project:' || substring(key from 8) "
            "WHERE key LIKE 'agents:%'"
        )
    )
    # work: -> user:work:
    conn.execute(sa.text("UPDATE kv_entries SET key = 'user:' || key WHERE key LIKE 'work:%'"))
    # Catchall: global: prefix for anything not yet namespaced
    conn.execute(
        sa.text(
            "UPDATE kv_entries SET key = 'global:' || key "
            "WHERE key NOT LIKE 'global:%' "
            "AND key NOT LIKE 'user:%' "
            "AND key NOT LIKE 'project:%'"
        )
    )

    # --- Step 2: Drop old constraints and indexes ---
    if _index_exists('idx_kv_global_key'):
        op.drop_index('idx_kv_global_key', table_name='kv_entries')
    if _constraint_exists('uq_kv_vault_key'):
        op.drop_constraint('uq_kv_vault_key', 'kv_entries', type_='unique')
    if _index_exists('ix_kv_entries_vault_id'):
        op.drop_index('ix_kv_entries_vault_id', table_name='kv_entries')

    # --- Step 3: Drop vault_id column ---
    if _column_exists('kv_entries', 'vault_id'):
        op.drop_column('kv_entries', 'vault_id')

    # --- Step 4: Create new constraints ---
    if not _constraint_exists('uq_kv_key'):
        op.create_unique_constraint('uq_kv_key', 'kv_entries', ['key'])
    if not _index_exists('idx_kv_key_prefix'):
        op.create_index(
            'idx_kv_key_prefix',
            'kv_entries',
            ['key'],
            postgresql_using='btree',
            postgresql_ops={'key': 'text_pattern_ops'},
        )


def downgrade() -> None:
    # Drop new constraints
    if _index_exists('idx_kv_key_prefix'):
        op.drop_index('idx_kv_key_prefix', table_name='kv_entries')
    if _constraint_exists('uq_kv_key'):
        op.drop_constraint('uq_kv_key', 'kv_entries', type_='unique')

    # Re-add vault_id column
    op.add_column(
        'kv_entries',
        sa.Column(
            'vault_id',
            sa.UUID(),
            sa.ForeignKey('vaults.id', ondelete='CASCADE'),
            nullable=True,
        ),
    )
    op.create_index('ix_kv_entries_vault_id', 'kv_entries', ['vault_id'])

    # Re-create old constraints
    op.create_unique_constraint('uq_kv_vault_key', 'kv_entries', ['vault_id', 'key'])
    op.create_index(
        'idx_kv_global_key',
        'kv_entries',
        ['key'],
        unique=True,
        postgresql_where=sa.text('vault_id IS NULL'),
    )

    # Reverse key migrations (best-effort)
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE kv_entries SET key = substring(key from 8) WHERE key LIKE 'global:%'")
    )
    conn.execute(
        sa.text("UPDATE kv_entries SET key = substring(key from 6) WHERE key LIKE 'user:work:%'")
    )
    conn.execute(
        sa.text(
            "UPDATE kv_entries SET key = 'agents:' || substring(key from 9) "
            "WHERE key LIKE 'project:%'"
        )
    )
