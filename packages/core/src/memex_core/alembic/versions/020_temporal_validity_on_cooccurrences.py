"""Add valid_from and valid_to columns to entity_cooccurrences for temporal validity.

Nullable TIMESTAMP WITH TIME ZONE columns. NULL means open-ended (no bound).
Composite index on (entity_id_1, entity_id_2, valid_to DESC NULLS FIRST, valid_from DESC)
accelerates as-of temporal queries.

Revision ID: 020_temporal_cooccurrences
Revises: 019_kv_ttl
Create Date: 2026-04-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '020_temporal_cooccurrences'
down_revision: Union[str, None] = '019_kv_ttl'
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

    if not _column_exists(conn, 'entity_cooccurrences', 'valid_from'):
        op.add_column(
            'entity_cooccurrences',
            sa.Column('valid_from', sa.TIMESTAMP(timezone=True), nullable=True),
        )

    if not _column_exists(conn, 'entity_cooccurrences', 'valid_to'):
        op.add_column(
            'entity_cooccurrences',
            sa.Column('valid_to', sa.TIMESTAMP(timezone=True), nullable=True),
        )

    if not _index_exists(conn, 'idx_entity_cooccurrences_temporal'):
        op.create_index(
            'idx_entity_cooccurrences_temporal',
            'entity_cooccurrences',
            [
                'entity_id_1',
                'entity_id_2',
                sa.text('valid_to DESC NULLS FIRST'),
                sa.text('valid_from DESC'),
            ],
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_entity_cooccurrences_temporal'):
        op.drop_index('idx_entity_cooccurrences_temporal', table_name='entity_cooccurrences')

    if _column_exists(conn, 'entity_cooccurrences', 'valid_to'):
        op.drop_column('entity_cooccurrences', 'valid_to')

    if _column_exists(conn, 'entity_cooccurrences', 'valid_from'):
        op.drop_column('entity_cooccurrences', 'valid_from')
