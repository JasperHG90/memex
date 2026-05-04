"""mw_mode column on vaults (per-vault EMA opt-in).

Adds ``mw_mode TEXT NOT NULL DEFAULT 'stationary' CHECK (mw_mode IN ('stationary', 'ema'))``
to the ``vaults`` table. Default ``stationary`` — no behaviour change for existing
vaults.

Revision ID: 034_add_mw_mode
Revises: 033_confidence_evidence_count
Create Date: 2026-05-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '034_add_mw_mode'
down_revision: str | None = '033_confidence_evidence_count'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_CONSTRAINT_NAME = 'vaults_mw_mode_check'


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

    if not _column_exists(conn, 'vaults', 'mw_mode'):
        op.add_column(
            'vaults',
            sa.Column(
                'mw_mode',
                sa.Text(),
                nullable=False,
                server_default='stationary',
            ),
        )

    if not _constraint_exists(conn, _CHECK_CONSTRAINT_NAME):
        op.create_check_constraint(
            _CHECK_CONSTRAINT_NAME,
            'vaults',
            "mw_mode IN ('stationary', 'ema')",
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _constraint_exists(conn, _CHECK_CONSTRAINT_NAME):
        op.drop_constraint(_CHECK_CONSTRAINT_NAME, 'vaults', type_='check')

    if _column_exists(conn, 'vaults', 'mw_mode'):
        op.drop_column('vaults', 'mw_mode')
