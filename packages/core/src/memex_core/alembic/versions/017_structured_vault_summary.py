"""Restructure vault_summaries: summary→narrative, topics→themes, add inventory + key_entities.

Replaces the unstructured prose summary with a structured schema:
- narrative: short thematic synthesis (~200 tokens, replaces 750-token summary)
- themes: richer topic structure with trends (replaces topics)
- inventory: computed content stats (no LLM needed)
- key_entities: top entities by mention count (no LLM needed)
- stats column dropped (subsumed by inventory)

Revision ID: 017_structured_vault_summary
Revises: 016_note_summary_version
Create Date: 2026-04-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '017_structured_vault_summary'
down_revision: Union[str, None] = '016_note_summary_version'
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

    # Add new columns (skip if 001_full_baseline already created them from current model)
    if not _column_exists(conn, 'vault_summaries', 'narrative'):
        op.add_column(
            'vault_summaries',
            sa.Column('narrative', sa.Text(), server_default=sa.text("''"), nullable=False),
        )
    if not _column_exists(conn, 'vault_summaries', 'themes'):
        op.add_column(
            'vault_summaries',
            sa.Column(
                'themes',
                sa.dialects.postgresql.JSONB(),
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
    if not _column_exists(conn, 'vault_summaries', 'inventory'):
        op.add_column(
            'vault_summaries',
            sa.Column(
                'inventory',
                sa.dialects.postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
            ),
        )
    if not _column_exists(conn, 'vault_summaries', 'key_entities'):
        op.add_column(
            'vault_summaries',
            sa.Column(
                'key_entities',
                sa.dialects.postgresql.JSONB(),
                server_default=sa.text("'[]'::jsonb"),
            ),
        )

    # Migrate data and drop old columns (only if old columns exist)
    if _column_exists(conn, 'vault_summaries', 'summary'):
        op.execute(sa.text('UPDATE vault_summaries SET narrative = summary, themes = topics'))
        op.drop_column('vault_summaries', 'summary')

    if _column_exists(conn, 'vault_summaries', 'topics'):
        op.drop_column('vault_summaries', 'topics')

    if _column_exists(conn, 'vault_summaries', 'stats'):
        op.drop_column('vault_summaries', 'stats')


def downgrade() -> None:
    conn = op.get_bind()

    # Re-add old columns
    if not _column_exists(conn, 'vault_summaries', 'summary'):
        op.add_column(
            'vault_summaries',
            sa.Column('summary', sa.Text(), server_default=sa.text("''"), nullable=False),
        )
    if not _column_exists(conn, 'vault_summaries', 'topics'):
        op.add_column(
            'vault_summaries',
            sa.Column(
                'topics',
                sa.dialects.postgresql.JSONB(),
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
    if not _column_exists(conn, 'vault_summaries', 'stats'):
        op.add_column(
            'vault_summaries',
            sa.Column(
                'stats',
                sa.dialects.postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    # Migrate data back
    if _column_exists(conn, 'vault_summaries', 'narrative'):
        op.execute(sa.text('UPDATE vault_summaries SET summary = narrative, topics = themes'))
        op.drop_column('vault_summaries', 'narrative')

    if _column_exists(conn, 'vault_summaries', 'themes'):
        op.drop_column('vault_summaries', 'themes')

    if _column_exists(conn, 'vault_summaries', 'inventory'):
        op.drop_column('vault_summaries', 'inventory')

    if _column_exists(conn, 'vault_summaries', 'key_entities'):
        op.drop_column('vault_summaries', 'key_entities')
