"""Add partial covering index on ``nodes(vault_id)`` (B.2).

The nodes-keyword CTE in document_search filters on
``tsvector @@ ts_query AND block_id IS NOT NULL AND status='active'
AND vault_id IN (...)``. The existing tsvector GIN index covers the
fulltext predicate; the remaining ``status``/``block_id``/``vault_id``
filters were bitmap-rechecked because no index covers them. Under
realistic vault sizes this is a non-trivial fraction of the 18s
``statement_timeout`` documented in the 2026-05-29 tech report.

The new partial index narrows the bitmap to live, block-assigned rows
keyed by vault — exactly the predicate the hot CTE writes.

Revision ID: 054_nodes_vault_active
Revises: 053_merge_heads
Create Date: 2026-05-29
"""

import sqlalchemy as sa
from alembic import op

revision: str = '054_nodes_vault_active'
down_revision: str | None = '053_merge_heads'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_index(
        'idx_nodes_vault_active',
        'nodes',
        ['vault_id'],
        unique=False,
        postgresql_where=sa.text("status = 'active' AND block_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index('idx_nodes_vault_active', table_name='nodes')
