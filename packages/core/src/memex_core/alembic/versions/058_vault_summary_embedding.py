"""Add a nullable narrative embedding to vault_summaries.

The vault summary's narrative is the canonical vault-level text; storing its
embedding lets HTTP/Python callers compare memory units against the vault
without re-encoding the narrative per request. Populated lazily: the next
``update_summary`` / ``regenerate_summary`` write fills it, so pre-existing
rows stay NULL until their narrative is next rewritten. No index — the table
holds one row per vault and is read by its unique ``vault_id`` key, never
scanned by vector similarity.

Revision ID: 058_vault_summary_embedding
Revises: 057_lint_source_external
Create Date: 2026-06-07
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = '058_vault_summary_embedding'
down_revision: str | None = '057_lint_source_external'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.add_column(
        'vault_summaries',
        sa.Column('embedding', Vector(384), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('vault_summaries', 'embedding')
