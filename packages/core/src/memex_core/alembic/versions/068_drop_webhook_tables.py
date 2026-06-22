"""Drop the orphan webhook_* tables.

``webhook_registrations`` and ``webhook_deliveries`` were created in the
``001_full_baseline`` baseline but never wired to an ORM model or any query —
the live webhook surface is an HTTP ingestion path with no DB persistence. So
fresh installs (which bootstrap from ``SQLModel.metadata``) never get these
tables, while databases upgraded in place from an old release still carry them.
This migration drops them so existing DBs match a fresh install. Reversible:
``downgrade`` recreates both tables + indexes verbatim from the baseline.

Revision ID: 068_drop_webhook_tables
Revises: 067_add_skill_hints
Create Date: 2026-06-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP

revision: str = '068_drop_webhook_tables'
down_revision: str | None = '067_add_skill_hints'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # deliveries first — it has an FK to registrations. IF EXISTS keeps the
    # migration idempotent for any DB that already lacks the orphan tables.
    op.execute('DROP TABLE IF EXISTS webhook_deliveries')
    op.execute('DROP TABLE IF EXISTS webhook_registrations')


def downgrade() -> None:
    # Recreate verbatim from 001_full_baseline (registrations first for the FK).
    op.create_table(
        'webhook_registrations',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('url', sa.String(2048), nullable=False),
        sa.Column('secret', sa.String(255), nullable=False),
        sa.Column('events', ARRAY(sa.Text), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_webhook_registrations_active', 'webhook_registrations', ['active'])

    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column(
            'webhook_id',
            sa.Uuid(),
            sa.ForeignKey('webhook_registrations.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('event', sa.String(100), nullable=False),
        sa.Column('payload', JSONB, nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_webhook_deliveries_webhook_id', 'webhook_deliveries', ['webhook_id'])
    op.create_index('idx_webhook_deliveries_status', 'webhook_deliveries', ['status'])
