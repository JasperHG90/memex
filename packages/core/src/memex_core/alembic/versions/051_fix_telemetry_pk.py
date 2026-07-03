"""Fix lint_rule_telemetry PK — allow nullable vault_id.

The original migration (047) used a composite PK including vault_id,
which made vault_id implicitly NOT NULL. The global rollup (vault_id=NULL)
cannot be inserted. Fix: drop the composite PK, add a UUID id column as
PK, and make vault_id properly nullable.

Revision ID: 051_fix_telemetry_pk
Revises: 050_mp_flagged_at
Create Date: 2026-05-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = '051_fix_telemetry_pk'
down_revision: str | None = '050_mp_flagged_at'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.execute('DELETE FROM lint_rule_telemetry')

    op.drop_constraint('lint_rule_telemetry_pkey', 'lint_rule_telemetry', type_='primary')

    op.add_column(
        'lint_rule_telemetry',
        sa.Column(
            'id',
            UUID(as_uuid=True),
            server_default=sa.text('gen_random_uuid()'),
            nullable=False,
        ),
    )

    op.create_primary_key('lint_rule_telemetry_pkey', 'lint_rule_telemetry', ['id'])

    op.alter_column(
        'lint_rule_telemetry', 'vault_id', existing_type=UUID(as_uuid=True), nullable=True
    )

    op.create_unique_constraint(
        'uq_lint_rule_telemetry_rule_vault_window',
        'lint_rule_telemetry',
        ['rule_name', 'vault_id', 'window_start'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_lint_rule_telemetry_rule_vault_window', 'lint_rule_telemetry', type_='unique'
    )
    op.execute('DELETE FROM lint_rule_telemetry WHERE vault_id IS NULL')
    op.drop_constraint('lint_rule_telemetry_pkey', 'lint_rule_telemetry', type_='primary')
    op.drop_column('lint_rule_telemetry', 'id')
    op.alter_column(
        'lint_rule_telemetry', 'vault_id', existing_type=UUID(as_uuid=True), nullable=False
    )
    op.create_primary_key(
        'lint_rule_telemetry_pkey',
        'lint_rule_telemetry',
        ['rule_name', 'vault_id', 'window_start'],
    )
