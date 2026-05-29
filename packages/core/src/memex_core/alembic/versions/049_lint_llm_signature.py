"""Add lint_llm_signature table — Layer 4 of the lint auto-learning loop.

Stores versioned compiled DSPy signatures. The weekly optimizer reads
labelled verdicts, compiles a new signature, validates against a
champion, and promotes here. LLM checks load the latest unsuperseded
row per (rule_name, vault_id) at server startup.

Revision ID: 049_lint_llm_signature
Revises: 048_lint_rule_calibration
Create Date: 2026-05-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = '049_lint_llm_signature'
down_revision: str | None = '048_lint_rule_calibration'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.create_table(
        'lint_llm_signature',
        sa.Column(
            'id',
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column('rule_name', sa.Text(), nullable=False),
        sa.Column('vault_id', UUID(as_uuid=True), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('compiled_program', JSONB, nullable=True),
        sa.Column('demos', JSONB, nullable=True),
        sa.Column('base_model', sa.Text(), nullable=True),
        sa.Column('validation_score', sa.Float(), nullable=True),
        sa.Column('validation_examples', sa.Integer(), nullable=True),
        sa.Column(
            'promoted_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column('promoted_by', sa.Text(), nullable=True),
        sa.Column('superseded_by_version', sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            'rule_name',
            'vault_id',
            'version',
            name='uq_lint_llm_signature_rule_vault_version',
        ),
    )
    op.create_index(
        'idx_lint_llm_signature_active',
        'lint_llm_signature',
        ['rule_name', 'vault_id'],
        unique=False,
        postgresql_where=sa.text('superseded_by_version IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('idx_lint_llm_signature_active', table_name='lint_llm_signature')
    op.drop_table('lint_llm_signature')
