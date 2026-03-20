"""Add summary and summary_formatted columns to chunks table.

Persists block-level summaries (topic + key_points) generated during extraction.

Revision ID: 008_chunk_summary
Revises: 007_kv_namespace_prefix
Create Date: 2026-03-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '008_chunk_summary'
down_revision: Union[str, None] = '007_kv_namespace_prefix'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table AND column_name = :col'
        ),
        {'table': table, 'col': column},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _column_exists('chunks', 'summary'):
        op.add_column('chunks', sa.Column('summary', JSONB, nullable=True))
    if not _column_exists('chunks', 'summary_formatted'):
        op.add_column('chunks', sa.Column('summary_formatted', sa.Text, nullable=True))


def downgrade() -> None:
    if _column_exists('chunks', 'summary_formatted'):
        op.drop_column('chunks', 'summary_formatted')
    if _column_exists('chunks', 'summary'):
        op.drop_column('chunks', 'summary')
