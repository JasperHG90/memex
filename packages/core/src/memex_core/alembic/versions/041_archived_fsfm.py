"""Migrate ``Note.status='archived'`` to FSFM (archived_at + is_deprioritized).

Pre-migration both ``superseded`` and ``archived`` call
``_deactivate_note_units`` (cascading ``MemoryUnit.status='stale'``).
That conflates two distinct intents:

  * ``superseded`` = replacement (a new note carries the authoritative
    version).
  * ``archived`` = explicit human "soft delete" intent.

Re-implement ``archived`` on top of FSFM: cascade
``MemoryUnit.is_deprioritized=true`` rather than ``status='stale'``, and
record the human-intent signal in ``Note.archived_at``. ``archived`` is
dropped from the ``ck_notes_status`` CHECK enum — agent intent stays
the same (``set_note_status('archived')`` still works), but storage
moves to FSFM.

This migration:

  * Adds ``notes.archived_at`` (nullable timestamp w/ tz, indexed).
  * Backfills ``archived_at = updated_at`` for rows still at
    ``status='archived'``.
  * Cascades ``memory_units.is_deprioritized=true`` for units of those
    notes.
  * Flips those notes to ``status='active'`` so the new CHECK enum
    accepts them.
  * Recreates ``ck_notes_status`` without ``'archived'``.

Downgrade re-adds ``'archived'`` to the CHECK enum and drops
``archived_at``. **This downgrade is one-way for the data**: it does
NOT restore ``notes.status='archived'`` for rows the upgrade flipped
to ``'active'``, nor reset ``MemoryUnit.is_deprioritized=true`` — the
durable signals of the human intent moved to ``archived_at`` and
``is_deprioritized``, both of which are dropped/retained respectively
on rollback. After upgrade+downgrade, formerly-archived rows are now
``status='active'`` with deprioritized units and no signal of their
prior archived state. This is acceptable since rolling back is an
audit event, not a routine operation.

**Merge note**: a sibling branch introduces a migration also numbered
``041_*`` that drops ``'appended'`` from ``ck_notes_status``.
Whichever branch lands second must rebase its revision to ``042_*``,
chain ``down_revision`` off the first-merged ``041``, and recreate
``ck_notes_status`` with the cumulative drop (``IN ('active',
'superseded')``). The drops are commutative on the constraint but the
alembic revision graph is not.

Revision ID: 041_archived_fsfm
Revises: 040_outcome_per_unit_schema
Create Date: 2026-05-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP


revision: str = '041_archived_fsfm'
down_revision: Union[str, None] = '040_outcome_per_unit_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'notes',
        sa.Column('archived_at', TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index('ix_notes_archived_at', 'notes', ['archived_at'])

    op.execute(
        """
        UPDATE notes
           SET archived_at = updated_at
         WHERE status = 'archived'
           AND archived_at IS NULL
        """
    )

    op.execute(
        """
        UPDATE memory_units
           SET is_deprioritized = true,
               status = CASE WHEN status = 'stale' THEN 'active' ELSE status END
         WHERE note_id IN (SELECT id FROM notes WHERE status = 'archived')
        """
    )

    op.execute("UPDATE notes SET status = 'active' WHERE status = 'archived'")

    op.drop_constraint('ck_notes_status', 'notes', type_='check')
    op.create_check_constraint(
        'ck_notes_status',
        'notes',
        "status IN ('active', 'superseded', 'appended')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_notes_status', 'notes', type_='check')
    op.create_check_constraint(
        'ck_notes_status',
        'notes',
        "status IN ('active', 'superseded', 'appended', 'archived')",
    )
    op.drop_index('ix_notes_archived_at', table_name='notes')
    op.drop_column('notes', 'archived_at')
