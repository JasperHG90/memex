"""kind + policy columns on vaults (content vs system vaults).

Adds ``kind TEXT NOT NULL DEFAULT 'content' CHECK (kind IN ('content','system'))``
and ``policy JSONB NOT NULL DEFAULT '{}'`` to ``vaults``. Default ``content`` —
no behaviour change for existing vaults. One-shot: marks an existing ``inbox``
vault as ``system`` and archives its now-stale synthesis (mental models +
vault summary), since system vaults are not reflected/summarized by default.

Revision ID: 060_vault_kind_policy
Revises: 059_drop_inbox_router
Create Date: 2026-06-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '060_vault_kind_policy'
down_revision: str | None = '059_drop_inbox_router'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_CHECK = 'vaults_kind_check'
_INBOX_NAME = 'inbox'


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.columns'
            '  WHERE table_schema = current_schema()'
            '    AND table_name = :table AND column_name = :column'
            ')'
        ),
        {'table': table, 'column': column},
    )
    return bool(result.scalar())


def _constraint_exists(conn, name: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.table_constraints'
            '  WHERE table_schema = current_schema()'
            '    AND constraint_name = :name'
            ')'
        ),
        {'name': name},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, 'vaults', 'kind'):
        op.add_column(
            'vaults',
            sa.Column('kind', sa.Text(), nullable=False, server_default='content'),
        )

    if not _column_exists(conn, 'vaults', 'policy'):
        op.add_column(
            'vaults',
            sa.Column(
                'policy',
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    if not _constraint_exists(conn, _KIND_CHECK):
        op.create_check_constraint(
            _KIND_CHECK,
            'vaults',
            "kind IN ('content', 'system')",
        )

    # One-shot: a pre-existing inbox vault becomes system; its prior synthesis is
    # now stale (system vaults are not reflected/summarized by default).
    inbox_id = conn.execute(
        sa.text('SELECT id FROM vaults WHERE name = :name'), {'name': _INBOX_NAME}
    ).scalar()
    if inbox_id is not None:
        conn.execute(sa.text("UPDATE vaults SET kind = 'system' WHERE id = :id"), {'id': inbox_id})
        conn.execute(
            sa.text(
                'UPDATE mental_models SET archived_at = now() '
                'WHERE vault_id = :id AND archived_at IS NULL'
            ),
            {'id': inbox_id},
        )
        conn.execute(sa.text('DELETE FROM vault_summaries WHERE vault_id = :id'), {'id': inbox_id})


def downgrade() -> None:
    # Note: the inbox-synthesis archival is not reversed (archived mental models
    # stay archived; the deleted vault summary regenerates on the next sweep).
    conn = op.get_bind()

    if _constraint_exists(conn, _KIND_CHECK):
        op.drop_constraint(_KIND_CHECK, 'vaults', type_='check')

    if _column_exists(conn, 'vaults', 'policy'):
        op.drop_column('vaults', 'policy')

    if _column_exists(conn, 'vaults', 'kind'):
        op.drop_column('vaults', 'kind')
