"""F14 procedure_outcomes — vault-scoped MW counters for procedural KV keys.

Scoped to counters ONLY — active value/version/history live in KVEntry.value
JSON envelope (no schema change to kv_entries). One row per (vault_id, kv_key);
counters increment via OutcomeService.record_outcome(target_type='kv_key', ...).

FK to kv_entries.key ON DELETE CASCADE — deleting a procedure key cleans up its
counter rows. UniqueConstraint(vault_id, kv_key) enforces vault-scoped isolation
(Wave 0 invariant).

Revision ID: 028_procedure_outcomes
Revises: 027_consolidation_ticks
Create Date: 2026-04-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = '028_procedure_outcomes'
down_revision: Union[str, None] = '027_consolidation_ticks'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, table: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = :table)'
        ),
        {'table': table},
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

    if not _table_exists(conn, 'procedure_outcomes'):
        op.create_table(
            'procedure_outcomes',
            sa.Column(
                'id',
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column('vault_id', UUID(as_uuid=True), nullable=False),
            sa.Column('kv_key', sa.Text(), nullable=False),
            sa.Column(
                'success_co_count',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
            sa.Column(
                'failure_co_count',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
            sa.Column(
                'last_outcome_at',
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.Column(
                'created_at',
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                'updated_at',
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint('vault_id', 'kv_key', name='uq_procedure_outcomes_vault_key'),
            sa.ForeignKeyConstraint(
                ['kv_key'],
                ['kv_entries.key'],
                ondelete='CASCADE',
                name='fk_procedure_outcomes_kv_key',
            ),
        )

    if not _index_exists(conn, 'idx_procedure_outcomes_vault_key'):
        op.create_index(
            'idx_procedure_outcomes_vault_key',
            'procedure_outcomes',
            ['vault_id', 'kv_key'],
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_procedure_outcomes_vault_key'):
        op.drop_index('idx_procedure_outcomes_vault_key', table_name='procedure_outcomes')

    if _table_exists(conn, 'procedure_outcomes'):
        op.drop_table('procedure_outcomes')
