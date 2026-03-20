"""Add generated search_tsvector column and GIN index to memory_units.

Combines text, tags, enriched_tags, and enriched_keywords into a single
stored tsvector column for BM25 keyword search via KeywordStrategy.

Revision ID: 009_memory_units_search_tsvector
Revises: 008_chunk_summary
Create Date: 2026-03-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '009_memory_units_search_tsvector'
down_revision: Union[str, None] = '008_chunk_summary'
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


def _index_exists(index_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text('SELECT 1 FROM pg_indexes WHERE indexname = :name'),
        {'name': index_name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    if not _column_exists('memory_units', 'search_tsvector'):
        op.execute(
            sa.text(
                'ALTER TABLE memory_units ADD COLUMN search_tsvector tsvector '
                'GENERATED ALWAYS AS ('
                "to_tsvector('english', "
                "coalesce(text, '') || ' ' || "
                "coalesce(metadata->>'tags', '') || ' ' || "
                "coalesce(metadata->>'enriched_tags', '') || ' ' || "
                "coalesce(metadata->>'enriched_keywords', ''))"
                ') STORED'
            )
        )

    if not _index_exists('idx_memory_units_search_tsvector'):
        op.execute(
            sa.text(
                'CREATE INDEX idx_memory_units_search_tsvector '
                'ON memory_units USING gin (search_tsvector)'
            )
        )


def downgrade() -> None:
    if _index_exists('idx_memory_units_search_tsvector'):
        op.drop_index('idx_memory_units_search_tsvector', table_name='memory_units')
    if _column_exists('memory_units', 'search_tsvector'):
        op.drop_column('memory_units', 'search_tsvector')
