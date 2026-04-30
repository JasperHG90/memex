"""Add MW counters + is_deprioritized; remove dead access_count.

- Add success_co_count, failure_co_count, is_deprioritized to memory_units
- Add success_co_count, failure_co_count to unit_entities
- Add success_co_count, failure_co_count to mental_models
- Remove access_count column + index from memory_units (dead code: never written)
- Add partial index on is_deprioritized for fast default-scope queries

Revision ID: 023_mw_counters_and_deprioritize
Revises: 022_note_appends
Create Date: 2026-04-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '023_mw_counters_and_deprioritize'
down_revision: Union[str, None] = '022_note_appends'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.columns'
            '  WHERE table_name = :table AND column_name = :column'
            ')'
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

    # --- memory_units ---
    for col_name, col_type, server_default in [
        ('success_co_count', sa.Integer(), '0'),
        ('failure_co_count', sa.Integer(), '0'),
    ]:
        if not _column_exists(conn, 'memory_units', col_name):
            op.add_column(
                'memory_units',
                sa.Column(
                    col_name, col_type, server_default=sa.text(server_default), nullable=False
                ),
            )

    if not _column_exists(conn, 'memory_units', 'is_deprioritized'):
        op.add_column(
            'memory_units',
            sa.Column(
                'is_deprioritized',
                sa.Boolean(),
                server_default=sa.text('false'),
                nullable=False,
            ),
        )

    # Partial index for deprioritized units (default-scope queries hit the small set)
    if not _index_exists(conn, 'idx_memory_units_is_deprioritized'):
        op.create_index(
            'idx_memory_units_is_deprioritized',
            'memory_units',
            ['is_deprioritized'],
            postgresql_where=sa.text('is_deprioritized = true'),
        )

    # Remove dead access_count column + index
    if _index_exists(conn, 'idx_memory_units_access_count'):
        op.drop_index('idx_memory_units_access_count', table_name='memory_units')

    if _column_exists(conn, 'memory_units', 'access_count'):
        op.drop_column('memory_units', 'access_count')

    # --- unit_entities ---
    for col_name, col_type, server_default in [
        ('success_co_count', sa.Integer(), '0'),
        ('failure_co_count', sa.Integer(), '0'),
    ]:
        if not _column_exists(conn, 'unit_entities', col_name):
            op.add_column(
                'unit_entities',
                sa.Column(
                    col_name, col_type, server_default=sa.text(server_default), nullable=False
                ),
            )

    # --- mental_models ---
    for col_name, col_type, server_default in [
        ('success_co_count', sa.Integer(), '0'),
        ('failure_co_count', sa.Integer(), '0'),
    ]:
        if not _column_exists(conn, 'mental_models', col_name):
            op.add_column(
                'mental_models',
                sa.Column(
                    col_name, col_type, server_default=sa.text(server_default), nullable=False
                ),
            )


def downgrade() -> None:
    conn = op.get_bind()

    # --- mental_models ---
    for col_name in ('failure_co_count', 'success_co_count'):
        if _column_exists(conn, 'mental_models', col_name):
            op.drop_column('mental_models', col_name)

    # --- unit_entities ---
    for col_name in ('failure_co_count', 'success_co_count'):
        if _column_exists(conn, 'unit_entities', col_name):
            op.drop_column('unit_entities', col_name)

    # --- memory_units ---
    if _index_exists(conn, 'idx_memory_units_is_deprioritized'):
        op.drop_index('idx_memory_units_is_deprioritized', table_name='memory_units')

    for col_name in ('is_deprioritized', 'failure_co_count', 'success_co_count'):
        if _column_exists(conn, 'memory_units', col_name):
            op.drop_column('memory_units', col_name)

    # Restore access_count (downgrade path — column was dead, so just add it back empty)
    if not _column_exists(conn, 'memory_units', 'access_count'):
        op.add_column(
            'memory_units',
            sa.Column('access_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        )
        op.create_index(
            'idx_memory_units_access_count',
            'memory_units',
            ['access_count'],
            postgresql_ops={'access_count': 'DESC'},
        )
