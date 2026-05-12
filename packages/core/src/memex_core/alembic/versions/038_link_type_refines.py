"""Add 'refines' to the memory_links link_type CHECK constraint.

The contradiction-resolution lint surface introduces a new
``refines`` link type for cases where two memory units appear to
contradict at the topical level but are actually compatible
refinements. The lint rule rewrites the inbound ``contradicts``
link as ``refines`` so:

- the graph-pressure aggregate at services/lint.py drops to zero for
  that link (``refines`` weight is 0.0 in both the SQL CTE and the
  Python parity pass), and
- the audit trail records that a human-reviewed agent judged the two
  units to be refinements, not contradictions.

The upgrade is additive — drops and recreates the
``memory_links_link_type_check`` CHECK constraint with the new literal
appended. No data is modified. The downgrade drops ``refines`` from the
literal list; if any rows currently use ``refines`` it raises a clear
error directing the operator to re-classify them first.

Both upgrade and downgrade rely on the standard migration discipline:
migrations run with sole DB access (no concurrent writes mid-deploy).
The drop-and-recreate is therefore safe; a concurrent INSERT between
DROP CONSTRAINT and ADD CONSTRAINT would otherwise admit a row that
violates the new check, but the deploy contract precludes that.

Revision ID: 038_link_type_refines
Revises: 037_entity_last_merge_scan_at
Create Date: 2026-05-11
"""

from typing import Sequence, Union

from alembic import op


revision: str = '038_link_type_refines'
down_revision: Union[str, None] = '037_entity_last_merge_scan_at'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONSTRAINT_NAME = 'memory_links_link_type_check'

_LITERALS_WITH_REFINES = (
    "'temporal', 'semantic', 'entity', 'causes', 'caused_by', "
    "'enables', 'prevents', 'reinforces', 'weakens', 'contradicts', 'refines'"
)

_LITERALS_WITHOUT_REFINES = (
    "'temporal', 'semantic', 'entity', 'causes', 'caused_by', "
    "'enables', 'prevents', 'reinforces', 'weakens', 'contradicts'"
)


def upgrade() -> None:
    op.execute(f'ALTER TABLE memory_links DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}')
    op.execute(
        f'ALTER TABLE memory_links ADD CONSTRAINT {_CONSTRAINT_NAME} '
        f'CHECK (link_type IN ({_LITERALS_WITH_REFINES}))'
    )


def downgrade() -> None:
    refining_rows = (
        op.get_bind()
        .exec_driver_sql("SELECT COUNT(*) FROM memory_links WHERE link_type = 'refines'")
        .scalar()
    )
    if refining_rows:
        raise RuntimeError(
            f'Cannot downgrade: {refining_rows} memory_links row(s) still use '
            "link_type='refines'. Re-classify these rows (e.g. back to "
            "'contradicts' or 'semantic') before downgrading."
        )
    op.execute(f'ALTER TABLE memory_links DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}')
    op.execute(
        f'ALTER TABLE memory_links ADD CONSTRAINT {_CONSTRAINT_NAME} '
        f'CHECK (link_type IN ({_LITERALS_WITHOUT_REFINES}))'
    )
