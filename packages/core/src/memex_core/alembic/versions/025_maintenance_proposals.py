"""F6 maintenance_proposals — finding ledger for the rule-based linter.

Creates the `maintenance_proposals` table that stores findings emitted by the
F6 LintService. Schema follows RFC-003 §"`MaintenanceProposal` schema":

- `vault_id` is nullable (NULL = global findings; reserved for Tier B; v1
  emits no global findings — see RFC-003 §"Cross-vault scope").
- The 4 enum-style columns (`lint_type`, `target_type`, `status`, `source`)
  are TEXT with CHECK constraints; matches the F25 (024) precedent.
- A partial unique index on `(rule_name, target_type, target_id, vault_id)
  WHERE status = 'pending'` enforces idempotent re-runs (the rule engine
  uses `INSERT ... ON CONFLICT DO NOTHING`).

Revision ID: 025_maintenance_proposals
Revises: 024_intent_risk_classifier
Create Date: 2026-04-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Uuid as SA_UUID

revision: str = '025_maintenance_proposals'
down_revision: Union[str, None] = '024_intent_risk_classifier'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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

    if not _table_exists(conn, 'maintenance_proposals'):
        op.create_table(
            'maintenance_proposals',
            sa.Column(
                'id',
                SA_UUID(),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column(
                'vault_id',
                SA_UUID(),
                sa.ForeignKey('vaults.id', ondelete='CASCADE'),
                nullable=True,
            ),
            sa.Column('lint_type', sa.Text(), nullable=False),
            sa.Column('target_type', sa.Text(), nullable=False),
            sa.Column('target_id', sa.Text(), nullable=False),
            sa.Column('rule_name', sa.Text(), nullable=False),
            sa.Column(
                'evidence',
                JSONB,
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column('suggested_action', sa.Text(), nullable=False),
            sa.Column(
                'status',
                sa.Text(),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column(
                'source',
                sa.Text(),
                nullable=False,
                server_default=sa.text("'rule'"),
            ),
            sa.Column(
                'created_at',
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                'resolved_at',
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.CheckConstraint(
                "lint_type IN ('structural', 'quality', 'governance', 'schema')",
                name='ck_maintenance_proposals_lint_type',
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'resolved', 'dismissed')",
                name='ck_maintenance_proposals_status',
            ),
            sa.CheckConstraint(
                "source IN ('rule', 'llm')",
                name='ck_maintenance_proposals_source',
            ),
        )

    if not _index_exists(conn, 'uq_maintenance_proposals_pending'):
        op.execute(
            sa.text(
                'CREATE UNIQUE INDEX uq_maintenance_proposals_pending '
                'ON maintenance_proposals (rule_name, target_type, target_id, vault_id) '
                "WHERE status = 'pending'"
            )
        )

    if not _index_exists(conn, 'idx_maintenance_proposals_vault_status'):
        op.create_index(
            'idx_maintenance_proposals_vault_status',
            'maintenance_proposals',
            ['vault_id', 'status'],
        )

    if not _index_exists(conn, 'idx_maintenance_proposals_lint_type'):
        op.create_index(
            'idx_maintenance_proposals_lint_type',
            'maintenance_proposals',
            ['lint_type'],
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_maintenance_proposals_lint_type'):
        op.drop_index('idx_maintenance_proposals_lint_type', table_name='maintenance_proposals')
    if _index_exists(conn, 'idx_maintenance_proposals_vault_status'):
        op.drop_index('idx_maintenance_proposals_vault_status', table_name='maintenance_proposals')
    if _index_exists(conn, 'uq_maintenance_proposals_pending'):
        op.execute(sa.text('DROP INDEX IF EXISTS uq_maintenance_proposals_pending'))

    if _table_exists(conn, 'maintenance_proposals'):
        op.drop_table('maintenance_proposals')
