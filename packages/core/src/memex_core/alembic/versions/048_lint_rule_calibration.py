"""Add lint_rule_calibration table — Layer 3 of the lint auto-learning loop.

Stores versioned per-rule emission thresholds learned from operator
verdicts. The nightly calibration job reads ``lint_rule_telemetry``
(Layer 2), applies a simple accept-rate rule, and writes a new row
here. LLM checks read the latest unsuperseded row per
``(rule_name, vault_id)`` at emission time instead of the static
config default.

Revision ID: 048_lint_rule_calibration
Revises: 047_lint_rule_telemetry
Create Date: 2026-05-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = '048_lint_rule_calibration'
down_revision: str | None = '047_lint_rule_telemetry'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_table(
        'lint_rule_calibration',
        sa.Column(
            'id',
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column('rule_name', sa.Text(), nullable=False),
        sa.Column('vault_id', UUID(as_uuid=True), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('surprise_threshold', sa.Float(), nullable=True),
        sa.Column('polarity_threshold', sa.Float(), nullable=True),
        sa.Column(
            'learned_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column('learned_from_window_start', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('learned_from_window_end', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('superseded_by_version', sa.Integer(), nullable=True),
        sa.Column('frozen', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('rationale', JSONB, nullable=True),
        sa.UniqueConstraint(
            'rule_name', 'vault_id', 'version', name='uq_lint_calibration_rule_vault_version'
        ),
    )
    op.create_index(
        'idx_lint_calibration_active',
        'lint_rule_calibration',
        ['rule_name', 'vault_id'],
        unique=False,
        postgresql_where=sa.text('superseded_by_version IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('idx_lint_calibration_active', table_name='lint_rule_calibration')
    op.drop_table('lint_rule_calibration')
