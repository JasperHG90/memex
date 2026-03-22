"""Remove token_usage_logs table.

Token usage tracking is now handled by OpenTelemetry tracing
(auto-instrumented via LiteLLM) instead of a custom PostgreSQL table.

Revision ID: 011_remove_token_usage
Revises: 010_note_description
Create Date: 2026-03-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '011_remove_token_usage'
down_revision: Union[str, None] = '010_note_description'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    """Check if a table exists (handles fresh installs where it was never created)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT 1 FROM information_schema.tables '
            'WHERE table_name = :table AND table_schema = :schema'
        ),
        {'table': table, 'schema': 'public'},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if _table_exists('token_usage_logs'):
        op.drop_table('token_usage_logs')


def downgrade() -> None:
    op.create_table(
        'token_usage_logs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column(
            'timestamp',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            index=True,
        ),
        sa.Column('session_id', sa.String(), nullable=False, index=True),
        sa.Column('models', sa.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('cost', sa.Float(), nullable=True),
        sa.Column('is_cached', sa.Boolean(), default=False),
        sa.Column(
            'context_metadata',
            sa.dialects.postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
