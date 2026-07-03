"""maintenance_proposals resolved_by column.

Adds a nullable ``resolved_by`` TEXT column to ``maintenance_proposals`` so
the reconsolidate / consolidate / lint resolution paths can record the
actor that resolved or dismissed a finding (agent name, operator id, or
internal subsystem). Mirrors the existing ``resolved_at`` shape: nullable
while a proposal is ``pending``, populated when status flips to
``resolved`` or ``dismissed``.

Backfill policy: existing resolved/dismissed rows keep ``resolved_by``
NULL. Callers that resolve proposals after this migration set the column
explicitly; older rows surface NULL through the read path.

Revision ID: 031_proposal_resolved_by
Revises: 030_revisit_last_reviewed_at
Create Date: 2026-05-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '031_proposal_resolved_by'
down_revision: Union[str, None] = '030_revisit_last_reviewed_at'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, table: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.tables'
            '  WHERE table_schema = current_schema() AND table_name = :table'
            ')'
        ),
        {'table': table},
    )
    return bool(result.scalar())


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.columns'
            '  WHERE table_schema = current_schema()'
            '    AND table_name = :table'
            '    AND column_name = :column'
            ')'
        ),
        {'table': table, 'column': column},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, 'maintenance_proposals'):
        return

    if not _column_exists(conn, 'maintenance_proposals', 'resolved_by'):
        op.add_column(
            'maintenance_proposals',
            sa.Column('resolved_by', sa.Text(), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, 'maintenance_proposals'):
        return

    if _column_exists(conn, 'maintenance_proposals', 'resolved_by'):
        op.drop_column('maintenance_proposals', 'resolved_by')
