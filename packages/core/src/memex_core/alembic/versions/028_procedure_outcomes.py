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

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = '028_procedure_outcomes'
down_revision: str | None = '027_consolidation_ticks'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def _table_exists(conn, table: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.tables'
            '  WHERE table_schema = current_schema() AND table_name = :table'
            ')'
        ),
        {'table': table},
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
                ['vault_id'],
                ['vaults.id'],
                ondelete='CASCADE',
                name='fk_procedure_outcomes_vault_id',
            ),
            sa.ForeignKeyConstraint(
                ['kv_key'],
                ['kv_entries.key'],
                ondelete='CASCADE',
                name='fk_procedure_outcomes_kv_key',
            ),
        )

    # Note: ``UniqueConstraint('vault_id', 'kv_key')`` already creates a
    # btree index on those columns; no separate ``CREATE INDEX`` is needed.


def downgrade() -> None:
    conn = op.get_bind()

    if _table_exists(conn, 'procedure_outcomes'):
        op.drop_table('procedure_outcomes')
