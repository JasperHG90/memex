"""Add intent_class + risk_class to memory_units (F25 — write-time classifier).

- Add intent_class TEXT column (permanent | durable | ephemeral), default 'durable'
- Add risk_class TEXT column (none | sensitive | private | safety), default 'none'
- CHECK constraints enforce enum values at the DB level
- Partial index on risk_class='private' to keep default-scope queries fast
  (private units are the small set; default queries hit the large complement)

Revision ID: 024_intent_risk_classifier
Revises: 023_mw_counters_and_deprioritize
Create Date: 2026-04-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '024_intent_risk_classifier'
down_revision: Union[str, None] = '023_mw_counters_and_deprioritize'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.columns'
            '  WHERE table_schema = current_schema()'
            '    AND table_name = :table AND column_name = :column'
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


def _constraint_exists(conn, constraint: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.table_constraints'
            '  WHERE constraint_schema = current_schema()'
            '    AND constraint_name = :constraint'
            ')'
        ),
        {'constraint': constraint},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, 'memory_units', 'intent_class'):
        op.add_column(
            'memory_units',
            sa.Column(
                'intent_class',
                sa.Text(),
                server_default=sa.text("'durable'"),
                nullable=False,
            ),
        )

    if not _column_exists(conn, 'memory_units', 'risk_class'):
        op.add_column(
            'memory_units',
            sa.Column(
                'risk_class',
                sa.Text(),
                server_default=sa.text("'none'"),
                nullable=False,
            ),
        )

    if not _constraint_exists(conn, 'ck_memory_units_intent_class'):
        op.create_check_constraint(
            'ck_memory_units_intent_class',
            'memory_units',
            "intent_class IN ('permanent', 'durable', 'ephemeral')",
        )

    if not _constraint_exists(conn, 'ck_memory_units_risk_class'):
        op.create_check_constraint(
            'ck_memory_units_risk_class',
            'memory_units',
            "risk_class IN ('none', 'sensitive', 'private', 'safety')",
        )

    if not _index_exists(conn, 'idx_memory_units_risk_class_private'):
        op.create_index(
            'idx_memory_units_risk_class_private',
            'memory_units',
            ['risk_class'],
            postgresql_where=sa.text("risk_class = 'private'"),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_memory_units_risk_class_private'):
        op.drop_index('idx_memory_units_risk_class_private', table_name='memory_units')

    if _constraint_exists(conn, 'ck_memory_units_risk_class'):
        op.drop_constraint('ck_memory_units_risk_class', 'memory_units', type_='check')

    if _constraint_exists(conn, 'ck_memory_units_intent_class'):
        op.drop_constraint('ck_memory_units_intent_class', 'memory_units', type_='check')

    if _column_exists(conn, 'memory_units', 'risk_class'):
        op.drop_column('memory_units', 'risk_class')

    if _column_exists(conn, 'memory_units', 'intent_class'):
        op.drop_column('memory_units', 'intent_class')
