"""Materialize a stored tsvector on nodes and chunks.

The document_search keyword CTE filtered/ranked with
``to_tsvector('english', text)`` computed at query time, backed by a *functional*
GIN index. Because GIN is lossy, the Bitmap Heap Scan recheck recomputed
``to_tsvector(text)`` per matched row, and ``ts_rank_cd`` recomputed it again for
ranking — ~500ms for a broad query matching thousands of rows. ``memory_units``
already avoids this with a stored ``search_tsvector`` column; this brings nodes and
chunks in line.

Adds a ``GENERATED ALWAYS AS (...) STORED`` column (Postgres backfills existing
rows on add) and swaps the GIN index from the functional expression to the stored
column. The generation expression is identical to the prior functional index
(``to_tsvector('english', coalesce(text, ''))``), so keyword results/ranking are
unchanged — only the per-row recompute is removed. Reversible.

Revision ID: 069_nodes_chunks_search_tsvector
Revises: 068_drop_webhook_tables
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '069_nodes_chunks_search_tsvector'
down_revision: str | None = '068_drop_webhook_tables'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GEN = "GENERATED ALWAYS AS (to_tsvector('english', coalesce(text, ''))) STORED"


def upgrade() -> None:
    # Stored generated tsvector columns. Postgres computes them for every existing
    # row at ADD COLUMN time (nodes ~17k, chunks ~3k — fast).
    op.execute(f'ALTER TABLE nodes ADD COLUMN search_tsvector tsvector {_GEN}')
    op.execute(f'ALTER TABLE chunks ADD COLUMN search_tsvector tsvector {_GEN}')

    # Swap the GIN index off the functional expression onto the stored column.
    op.drop_index('idx_nodes_text_tsvector', table_name='nodes')
    op.drop_index('idx_chunks_text_tsvector', table_name='chunks')
    op.create_index(
        'idx_nodes_search_tsvector', 'nodes', ['search_tsvector'], postgresql_using='gin'
    )
    op.create_index(
        'idx_chunks_search_tsvector', 'chunks', ['search_tsvector'], postgresql_using='gin'
    )


def downgrade() -> None:
    op.drop_index('idx_chunks_search_tsvector', table_name='chunks')
    op.drop_index('idx_nodes_search_tsvector', table_name='nodes')
    op.execute('ALTER TABLE chunks DROP COLUMN search_tsvector')
    op.execute('ALTER TABLE nodes DROP COLUMN search_tsvector')
    op.create_index(
        'idx_nodes_text_tsvector',
        'nodes',
        [sa.text("to_tsvector('english', text)")],
        postgresql_using='gin',
    )
    op.create_index(
        'idx_chunks_text_tsvector',
        'chunks',
        [sa.text("to_tsvector('english', text)")],
        postgresql_using='gin',
    )
