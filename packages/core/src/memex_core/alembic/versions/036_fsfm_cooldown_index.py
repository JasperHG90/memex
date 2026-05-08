"""Add partial index for FSFM auto-band cooldown query.

The FSFM auto-band reads ``audit_logs`` to find ``memory_restore``
events within the last ``cooldown_days`` (default 14). With existing
indexes the planner had to choose between the action index (broad,
covers all actions) and the timestamp index (broad, covers all
events) — both selective only when combined.

This partial index is tightly scoped: only rows with
``action='memory_restore'`` AND ``resource_type='memory_unit'``,
ordered by ``timestamp DESC`` so the cooldown range scan stops as
soon as it crosses the cutoff. Includes ``resource_id`` so the
cooldown SELECT is index-only.

The index is small (memory restore events are rare) so the write-
amplification cost is negligible.

Revision ID: 036_fsfm_cooldown_index
Revises: 035_drop_fsrs_revisit_columns
Create Date: 2026-05-08
"""

from typing import Sequence, Union

from alembic import op


revision: str = '036_fsfm_cooldown_index'
down_revision: Union[str, None] = '035_drop_fsrs_revisit_columns'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = 'idx_audit_logs_memory_restore_cooldown'


def upgrade() -> None:
    op.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {_INDEX_NAME}
        ON audit_logs (timestamp DESC, resource_id)
        WHERE action = 'memory_restore'
          AND resource_type = 'memory_unit'
        """
    )


def downgrade() -> None:
    op.execute(f'DROP INDEX IF EXISTS {_INDEX_NAME}')
