"""lint_llm_quota — rolling-24h cost cap counter.

Implements the hour-bucket-per-vault storage shape. One row per
(vault_id, hour_bucket); the 24h rolling window is computed by summing
the last 24 hour-buckets via an indexed range scan.

- ``vault_id`` FK to vaults.id ON DELETE CASCADE — deleting a vault drops
  its quota history.
- ``hour_bucket`` is UTC, truncated to the hour (clients MUST normalise).
- ``count`` is the number of LLM calls made in that hour for that vault.
- ``UNIQUE(vault_id, hour_bucket)`` enables idempotent UPSERT
  (``INSERT ... ON CONFLICT (vault_id, hour_bucket) DO UPDATE SET count = count + 1``).
- Index on ``(vault_id, hour_bucket)`` makes the rolling-window sum a
  bounded-range scan (≤24 rows / vault).

Volume: 24 rows/vault/day = ~8.7K rows/year/vault — negligible. No prune
required for years; deletion cascades on vault drop handles tenancy
cleanup.

Revision ID: 029_lint_llm_quota
Revises: 028_procedure_outcomes
Create Date: 2026-05-01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.types import Uuid as SA_UUID

revision: str = '029_lint_llm_quota'
down_revision: str | None = '028_procedure_outcomes'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


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


def _index_exists(conn, index: str) -> bool:
    result = conn.execute(
        sa.text('SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :index)'),
        {'index': index},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, 'lint_llm_quota'):
        op.create_table(
            'lint_llm_quota',
            sa.Column(
                'id',
                SA_UUID(),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column(
                'vault_id',
                SA_UUID(),
                sa.ForeignKey('vaults.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column(
                'hour_bucket',
                sa.TIMESTAMP(timezone=True),
                nullable=False,
            ),
            sa.Column(
                'count',
                sa.Integer(),
                nullable=False,
                server_default=sa.text('0'),
            ),
            sa.UniqueConstraint('vault_id', 'hour_bucket', name='uq_lint_llm_quota_vault_hour'),
            sa.CheckConstraint('count >= 0', name='ck_lint_llm_quota_count_non_negative'),
        )

    if not _index_exists(conn, 'idx_lint_llm_quota_vault_hour'):
        op.create_index(
            'idx_lint_llm_quota_vault_hour',
            'lint_llm_quota',
            ['vault_id', 'hour_bucket'],
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_lint_llm_quota_vault_hour'):
        op.drop_index('idx_lint_llm_quota_vault_hour', table_name='lint_llm_quota')

    if _table_exists(conn, 'lint_llm_quota'):
        op.drop_table('lint_llm_quota')
