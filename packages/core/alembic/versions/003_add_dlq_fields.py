"""Add retry_count, max_retries, last_error to reflection_queue and update status constraint.

Revision ID: 003
Revises: 002
Create Date: 2026-02-28
"""

from alembic import op
import sqlalchemy as sa

revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'reflection_queue',
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'reflection_queue',
        sa.Column('max_retries', sa.Integer(), nullable=False, server_default='3'),
    )
    op.add_column(
        'reflection_queue',
        sa.Column('last_error', sa.Text(), nullable=True),
    )

    # Update the CHECK constraint to include 'dead_letter'
    op.execute(
        'ALTER TABLE reflection_queue DROP CONSTRAINT IF EXISTS "ck_reflection_queue_status"'
    )
    # Also try the auto-generated constraint name pattern
    op.execute(
        'ALTER TABLE reflection_queue DROP CONSTRAINT IF EXISTS "reflection_queue_status_check"'
    )
    op.create_check_constraint(
        'reflection_queue_status_check',
        'reflection_queue',
        "status IN ('pending', 'processing', 'failed', 'dead_letter')",
    )


def downgrade() -> None:
    # Revert any dead_letter items to failed before restoring constraint
    op.execute("UPDATE reflection_queue SET status = 'failed' WHERE status = 'dead_letter'")

    op.execute(
        'ALTER TABLE reflection_queue DROP CONSTRAINT IF EXISTS "reflection_queue_status_check"'
    )
    op.create_check_constraint(
        'reflection_queue_status_check',
        'reflection_queue',
        "status IN ('pending', 'processing', 'failed')",
    )

    op.drop_column('reflection_queue', 'last_error')
    op.drop_column('reflection_queue', 'max_retries')
    op.drop_column('reflection_queue', 'retry_count')
