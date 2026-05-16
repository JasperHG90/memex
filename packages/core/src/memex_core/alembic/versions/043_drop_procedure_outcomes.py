"""Drop procedure_outcomes — procedure KV entries no longer carry Memory Worth.

Procedures are stored recipes (procedure:<verb>:<context-tag> in KV); they
are looked up by exact key, not relevance-ranked. The per-(vault, kv_key)
success/failure counters introduced in 028 were paired-writes the agent
issued via memex_record_outcome(target_type='kv_key', ...). That whole
mode is removed alongside this table — see the matching surface changes
across services/outcomes, server/outcomes, mcp/server, hermes plugin,
and agent_surface.

Downgrade re-creates the table empty. Counter history is NOT restored —
the source data does not survive a drop; downgrading is a schema-only
rollback. Snapshot exports created after this migration will not include
procedure_outcomes rows.

Revision ID: 043_drop_procedure_outcomes
Revises: 042_drop_note_status_appended
Create Date: 2026-05-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = '043_drop_procedure_outcomes'
down_revision: str | None = '042_drop_note_status_appended'
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
    if _table_exists(conn, 'procedure_outcomes'):
        op.drop_table('procedure_outcomes')


def downgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, 'procedure_outcomes'):
        return
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
        sa.Column('success_co_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('failure_co_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('last_outcome_at', sa.TIMESTAMP(timezone=True), nullable=True),
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
