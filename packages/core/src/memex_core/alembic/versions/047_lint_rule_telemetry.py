"""Add lint_rule_telemetry table — Layer 2 of the lint auto-learning loop.

Rolls up resolved proposals into per-rule / per-vault / per-window
aggregates. Read by ``memex lint stats``; later phases (threshold
calibration, DSPy compile) read the same table to decide whether they
have enough data to act. Vault-scoped by default; ``vault_id IS NULL``
holds the global rollup across vaults.

Revision ID: 047_lint_rule_telemetry
Revises: 046_mental_models_archived_at
Create Date: 2026-05-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = '047_lint_rule_telemetry'
down_revision: str | None = '046_mental_models_archived_at'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_table(
        'lint_rule_telemetry',
        sa.Column('rule_name', sa.Text(), nullable=False),
        # vault_id NULL => global rollup across vaults.
        sa.Column('vault_id', UUID(as_uuid=True), nullable=True),
        sa.Column('window_start', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('window_end', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('accept_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('no_op_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('dismiss_count', sa.Integer(), nullable=False, server_default='0'),
        # Pre-cockpit rows (no resolution.followup block). Counted separately so
        # operators can see how much history is unlabelled vs labelled.
        sa.Column('legacy_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('median_surprise', sa.Float(), nullable=True),
        sa.Column('median_time_to_resolve_seconds', sa.Integer(), nullable=True),
        sa.Column(
            'refreshed_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # The PK has nullable vault_id, which Postgres treats as distinct from
        # any other NULL. That is the intended semantic: at most one global
        # rollup per (rule_name, window_start). Postgres 15+ supports
        # NULLS NOT DISTINCT but the older default is fine here — the
        # refresh service uses an explicit DELETE+INSERT inside a transaction
        # for the global row, so the duplicate-NULL risk does not arise.
        sa.PrimaryKeyConstraint('rule_name', 'vault_id', 'window_start'),
    )
    # Fast lookup paths used by ``memex lint stats``: by rule, by vault, and
    # by accept_rate (which we compute on the fly — no index helps directly
    # but window_end DESC supports the "latest window first" listing).
    op.create_index(
        'idx_lint_rule_telemetry_rule_window',
        'lint_rule_telemetry',
        ['rule_name', sa.text('window_end DESC')],
        unique=False,
    )
    op.create_index(
        'idx_lint_rule_telemetry_vault_window',
        'lint_rule_telemetry',
        ['vault_id', sa.text('window_end DESC')],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_lint_rule_telemetry_vault_window', table_name='lint_rule_telemetry')
    op.drop_index('idx_lint_rule_telemetry_rule_window', table_name='lint_rule_telemetry')
    op.drop_table('lint_rule_telemetry')
