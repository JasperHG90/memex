"""Reflection-queue extensions for surgical observation refresh.

Adds the columns + partial indices needed for the V21 "deprioritization leak
via mental-model observations" fix:

  * ``task_type TEXT NOT NULL DEFAULT 'reflect'`` — discriminator between full
    reflect cycles and surgical observation-refresh tasks.
  * ``observation_id UUID NULL`` — payload for refresh tasks (the observation
    inside ``mental_models.observations`` to refresh).
  * ``priority_lane BOOLEAN NOT NULL DEFAULT FALSE`` — claim-ordering lane;
    refresh tasks and restore-driven priority reflects ride the priority lane.
  * ``source_unit_id UUID NULL`` — the MU whose deprio triggered the refresh;
    used by the post-lock sibling-query re-check to verify the citation is
    still present before re-synthesis.
  * Partial CHECK ``task_type IN ('reflect', 'refresh_observation')``.
  * Partial index ``idx_reflection_queue_lane_priority`` ordered by
    ``(priority_lane DESC, priority_score DESC, last_queued_at NULLS FIRST)``
    on ``status IN ('pending', 'failed')`` — covers the claim_next_batch
    query directly, including the backoff filter.
  * Partial UNIQUE ``idx_reflection_queue_refresh_unique`` on
    ``(entity_id, vault_id, observation_id) WHERE task_type =
    'refresh_observation' AND status IN ('pending', 'processing')`` —
    supports ``ON CONFLICT DO NOTHING`` dedupe across pending+processing.
  * Partial UNIQUE ``idx_reflection_queue_entity_vault_active_unique`` on
    ``(entity_id, vault_id) WHERE task_type = 'reflect' AND status IN
    ('pending', 'processing')`` — supports the restore-path
    ``enqueue_priority_reflect`` upsert.
  * DROP the old non-unique ``idx_reflection_queue_entity_vault`` composite
    (the new partial UNIQUE covers the active-row lookups it served).

Revision ID: 043_reflection_queue_refresh_task
Revises: 042_drop_note_status_appended
Create Date: 2026-05-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '043_reflection_queue_refresh_task'
down_revision: Union[str, None] = '042_drop_note_status_appended'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'reflection_queue',
        sa.Column('task_type', sa.Text(), nullable=False, server_default=sa.text("'reflect'")),
    )
    op.add_column(
        'reflection_queue',
        sa.Column('observation_id', sa.dialects.postgresql.UUID(), nullable=True),
    )
    op.add_column(
        'reflection_queue',
        sa.Column(
            'priority_lane',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )
    op.add_column(
        'reflection_queue',
        sa.Column('source_unit_id', sa.dialects.postgresql.UUID(), nullable=True),
    )

    op.create_check_constraint(
        'ck_reflection_queue_task_type',
        'reflection_queue',
        "task_type IN ('reflect', 'refresh_observation')",
    )

    # Claim-ordering partial index covers status IN ('pending', 'failed')
    # — both states `claim_next_batch` reclaims. IF NOT EXISTS keeps the
    # upgrade idempotent on partial-apply retry (downgrade uses IF EXISTS).
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_reflection_queue_lane_priority '
        'ON reflection_queue (priority_lane DESC, priority_score DESC, last_queued_at) '
        "WHERE status IN ('pending', 'failed')"
    )

    # Refresh-task dedupe: covers pending + processing so a PENDING->PROCESSING
    # transition doesn't let a duplicate slip in mid-flight.
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_reflection_queue_refresh_unique '
        'ON reflection_queue (entity_id, vault_id, observation_id) '
        "WHERE task_type = 'refresh_observation' AND status IN ('pending', 'processing')"
    )

    # Restore-path upsert target for enqueue_priority_reflect.
    op.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_reflection_queue_entity_vault_active_unique '
        'ON reflection_queue (entity_id, vault_id) '
        "WHERE task_type = 'reflect' AND status IN ('pending', 'processing')"
    )

    # Fresh-install baseline removed this index in the same commit, so it may
    # not exist on a clean DB. Use IF EXISTS so upgrade is safe in both cases.
    op.execute('DROP INDEX IF EXISTS idx_reflection_queue_entity_vault')


def downgrade() -> None:
    # Mirror the upgrade's IF EXISTS guard with IF NOT EXISTS — downgrades
    # may run from states where this index already exists (partial-rollback
    # scenarios), and the symmetric form keeps both directions idempotent.
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_reflection_queue_entity_vault '
        'ON reflection_queue (entity_id, vault_id)'
    )

    op.execute('DROP INDEX IF EXISTS idx_reflection_queue_entity_vault_active_unique')
    op.execute('DROP INDEX IF EXISTS idx_reflection_queue_refresh_unique')
    op.execute('DROP INDEX IF EXISTS idx_reflection_queue_lane_priority')

    op.drop_constraint('ck_reflection_queue_task_type', 'reflection_queue', type_='check')

    op.drop_column('reflection_queue', 'source_unit_id')
    op.drop_column('reflection_queue', 'priority_lane')
    op.drop_column('reflection_queue', 'observation_id')
    op.drop_column('reflection_queue', 'task_type')
