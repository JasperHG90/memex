"""Add needs_regeneration flag to vault_summaries.

Boolean column (default false) that signals when a vault summary
must be fully regenerated instead of incrementally updated — set
when notes are deleted or archived.

Revision ID: 018_vault_summary_needs_regen
Revises: 017_structured_vault_summary
Create Date: 2026-04-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '018_vault_summary_needs_regen'
down_revision: Union[str, None] = '017_structured_vault_summary'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table AND column_name = :column)'
        ),
        {'table': table, 'column': column},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, 'vault_summaries', 'needs_regeneration'):
        op.add_column(
            'vault_summaries',
            sa.Column(
                'needs_regeneration',
                sa.Boolean(),
                server_default=sa.text('false'),
                nullable=False,
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn, 'vault_summaries', 'needs_regeneration'):
        op.drop_column('vault_summaries', 'needs_regeneration')
