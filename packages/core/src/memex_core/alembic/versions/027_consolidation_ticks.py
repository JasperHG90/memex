"""F38 consolidation_ticks — vault-scoped tick-summary rows for consolidation orchestrator.

One row per `consolidation_tick(vault_id)` invocation. F38's `services/consolidation.py`
is a thin orchestrator over reflection + contradiction + prune-stale-only; its sole
DB write is the row inserted into this table at the end of each tick (AC-F38-4).

Schema per RFC-010 §"Tick-summary row schema":
- started_at + completed_at (NOT just last_tick_at — the gap captures wall-clock duration
  + signals "in progress" via NULL completed_at)
- units_processed: count of units returned by select_diff_units (capped at 500)
- entities_reflected, contradictions_run, stale_pruned: per-step counts
- error: optional free-text on failure (warning-level, not raise)

Two indexes:
- (vault_id, started_at) for ad-hoc per-vault history queries
- (vault_id, completed_at DESC NULLS LAST) covers `get_last_tick_timestamp` lookups
  via MAX(completed_at) — peer-review tweak from staff-eng-2.

Revision ID: 027_consolidation_ticks
Revises: 026_revisit_columns
Create Date: 2026-04-30
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = '027_consolidation_ticks'
down_revision: str | None = '026_revisit_columns'
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


def _index_exists(conn, index: str) -> bool:
    result = conn.execute(
        sa.text('SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :index)'),
        {'index': index},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, 'consolidation_ticks'):
        op.create_table(
            'consolidation_ticks',
            sa.Column(
                'id',
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column('vault_id', UUID(as_uuid=True), nullable=False),
            sa.Column('started_at', sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column('completed_at', sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column(
                'units_processed',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
            sa.Column(
                'entities_reflected',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
            sa.Column(
                'contradictions_run',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
            sa.Column(
                'stale_pruned',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
            sa.Column('error', sa.Text(), nullable=True),
            sa.Column(
                'created_at',
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ['vault_id'],
                ['vaults.id'],
                ondelete='CASCADE',
                name='fk_consolidation_ticks_vault_id',
            ),
        )

    if not _index_exists(conn, 'idx_consolidation_ticks_vault_started'):
        op.create_index(
            'idx_consolidation_ticks_vault_started',
            'consolidation_ticks',
            ['vault_id', 'started_at'],
        )

    if not _index_exists(conn, 'idx_consolidation_ticks_vault_completed'):
        op.create_index(
            'idx_consolidation_ticks_vault_completed',
            'consolidation_ticks',
            ['vault_id', sa.text('completed_at DESC NULLS LAST')],
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_consolidation_ticks_vault_completed'):
        op.drop_index(
            'idx_consolidation_ticks_vault_completed',
            table_name='consolidation_ticks',
        )

    if _index_exists(conn, 'idx_consolidation_ticks_vault_started'):
        op.drop_index(
            'idx_consolidation_ticks_vault_started',
            table_name='consolidation_ticks',
        )

    if _table_exists(conn, 'consolidation_ticks'):
        op.drop_table('consolidation_ticks')
