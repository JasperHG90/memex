"""Add ``nodes.assets`` JSONB column for per-section image references.

Page-index nodes (sections) can embed images via markdown / wiki-link /
HTML ``<img>`` syntax. This column stores structured metadata for each
ref — ``{"path", "alt_text", "filename"}`` — parsed at
ingest. It surfaces on ``NodeDTO.assets`` and on the page-index TOC so an
agent can see which sections own which images without an extra call.

Additive single-column ALTER, no new table. The existing
``notes.assets`` ARRAY (note-scoped attachments) is unchanged.

Revision ID: 056_node_assets
Revises: 055_inbox_router
Create Date: 2026-05-31
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '056_node_assets'
down_revision: str | None = '055_inbox_router'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    op.add_column(
        'nodes',
        sa.Column(
            'assets',
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column('nodes', 'assets')
