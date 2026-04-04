"""Add vault_summaries table.

Stores evolving vault-level summaries with topics, stats, patch log,
and an optional embedding vector for semantic retrieval.

Revision ID: 013_vault_summaries
Revises: 012_note_archived_status
Create Date: 2026-04-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = '013_vault_summaries'
down_revision: Union[str, None] = '012_note_archived_status'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIMENSION = 384


def upgrade() -> None:
    op.create_table(
        'vault_summaries',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column(
            'vault_id',
            sa.Uuid(),
            sa.ForeignKey('vaults.id', ondelete='CASCADE'),
            unique=True,
            nullable=False,
        ),
        sa.Column('summary', sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column('topics', sa.dialects.postgresql.JSONB(), server_default=sa.text("'[]'::jsonb")),
        sa.Column('stats', sa.dialects.postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column('version', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('notes_incorporated', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('last_note_id', sa.Uuid(), nullable=True),
        sa.Column(
            'patch_log', sa.dialects.postgresql.JSONB(), server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column('embedding', Vector(EMBEDDING_DIMENSION), nullable=True),
        sa.Column(
            'created_at',
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at',
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table('vault_summaries')
