"""Drop ``'appended'`` from ``Note.status`` CHECK enum.

``appended`` is a chronology marker, not a lifecycle suppression state.
The ``Note.appended_to`` FK and the ``NoteAppend`` table already encode
the parent-child relation, so the duplicate ``status='appended'`` value
carries no signal. This migration:

  * Backfills ``status='active'`` for any existing rows still at
    ``status='appended'`` (their memory units are already at
    ``status='active'`` so no cascade is needed; the FK on
    ``appended_to`` is preserved).
  * Recreates ``ck_notes_status`` without ``'appended'``.

Downgrade re-adds ``'appended'`` to the CHECK enum. No data is restored
on downgrade — rows backfilled to ``'active'`` stay at ``'active'``;
their parent FK in ``appended_to`` is the durable signal.

Revision ID: 041_drop_note_status_appended
Revises: 040_outcome_per_unit_schema
Create Date: 2026-05-14
"""

from typing import Sequence, Union

from alembic import op


revision: str = '041_drop_note_status_appended'
down_revision: Union[str, None] = '040_outcome_per_unit_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE notes SET status = 'active' WHERE status = 'appended'")
    op.drop_constraint('ck_notes_status', 'notes', type_='check')
    op.create_check_constraint(
        'ck_notes_status',
        'notes',
        "status IN ('active', 'superseded', 'archived')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_notes_status', 'notes', type_='check')
    op.create_check_constraint(
        'ck_notes_status',
        'notes',
        "status IN ('active', 'superseded', 'appended', 'archived')",
    )
