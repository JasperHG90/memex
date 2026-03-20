"""Move entity_metadata from entities to mental_models.

Revision ID: 005_entity_meta_to_model
Revises: 004_note_status
Create Date: 2026-03-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '005_entity_meta_to_model'
down_revision: Union[str, None] = '004_note_status'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check if a column already exists (handles baseline create_all)."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table AND column_name = :column'
        ),
        {'table': table, 'column': column},
    )
    return result.scalar() is not None


def upgrade() -> None:
    # Add entity_metadata to mental_models
    if not _column_exists('mental_models', 'entity_metadata'):
        op.add_column(
            'mental_models',
            sa.Column(
                'entity_metadata',
                sa.dialects.postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
        )

    # Drop metadata column from entities
    if _column_exists('entities', 'metadata'):
        op.drop_column('entities', 'metadata')


def downgrade() -> None:
    # Restore metadata column on entities
    if not _column_exists('entities', 'metadata'):
        op.add_column(
            'entities',
            sa.Column(
                'metadata',
                sa.dialects.postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
                nullable=True,
            ),
        )

    # Drop entity_metadata from mental_models
    if _column_exists('mental_models', 'entity_metadata'):
        op.drop_column('mental_models', 'entity_metadata')
