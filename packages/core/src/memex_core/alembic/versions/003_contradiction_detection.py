"""Add contradiction detection support.

Revision ID: 003_contradiction_detection
Revises: 002_remove_opinions_rename_event
Create Date: 2026-03-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '003_contradiction_detection'
down_revision: Union[str, None] = '002_remove_opinions_rename_event'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check if a column already exists (handles baseline create_all)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table AND column_name = :col'
        ),
        {'table': table, 'col': column},
    )
    return result.scalar() is not None


def _constraint_exists(name: str) -> bool:
    """Check if a constraint already exists."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text('SELECT 1 FROM pg_constraint WHERE conname = :name'),
        {'name': name},
    )
    return result.scalar() is not None


def _index_exists(name: str) -> bool:
    """Check if an index already exists."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text('SELECT 1 FROM pg_indexes WHERE indexname = :name'),
        {'name': name},
    )
    return result.scalar() is not None


def upgrade() -> None:
    # 1. Add confidence column to memory_units
    if not _column_exists('memory_units', 'confidence'):
        op.add_column(
            'memory_units',
            sa.Column('confidence', sa.Float(), nullable=False, server_default='1.0'),
        )
    if not _constraint_exists('memory_units_confidence_check'):
        op.create_check_constraint(
            'memory_units_confidence_check',
            'memory_units',
            'confidence >= 0.0 AND confidence <= 1.0',
        )
    if not _index_exists('idx_memory_units_confidence'):
        op.create_index('idx_memory_units_confidence', 'memory_units', ['confidence'])

    # 2. Add link_metadata JSONB column to memory_links
    if not _column_exists('memory_links', 'link_metadata'):
        op.add_column(
            'memory_links',
            sa.Column(
                'link_metadata',
                JSONB,
                server_default=sa.text("'{}'::jsonb"),
                nullable=True,
            ),
        )

    # 3. Update link_type CHECK constraint
    op.execute('ALTER TABLE memory_links DROP CONSTRAINT IF EXISTS memory_links_link_type_check')
    op.create_check_constraint(
        'memory_links_link_type_check',
        'memory_links',
        "link_type IN ('temporal', 'semantic', 'entity', 'causes', 'caused_by', "
        "'enables', 'prevents', 'reinforces', 'weakens', 'contradicts')",
    )


def downgrade() -> None:
    # Remove new link types by updating them first
    op.execute(
        "UPDATE memory_links SET link_type = 'semantic' "
        "WHERE link_type IN ('reinforces', 'weakens', 'contradicts')"
    )
    op.execute('ALTER TABLE memory_links DROP CONSTRAINT IF EXISTS memory_links_link_type_check')
    op.create_check_constraint(
        'memory_links_link_type_check',
        'memory_links',
        "link_type IN ('temporal', 'semantic', 'entity', 'causes', 'caused_by', "
        "'enables', 'prevents')",
    )
    op.drop_column('memory_links', 'link_metadata')
    op.drop_index('idx_memory_units_confidence', 'memory_units')
    op.execute('ALTER TABLE memory_units DROP CONSTRAINT IF EXISTS memory_units_confidence_check')
    op.drop_column('memory_units', 'confidence')
