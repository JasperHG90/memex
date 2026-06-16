"""Add structured skill_hints to procedural entries and versions.

Procedures can carry capability hints distilled from their steps. Storing
them as a structured list on the entry lets agents map them to local skills
without parsing markdown. The version ledger mirrors the column so every
edit preserves the hint snapshot.

Revision ID: 067_add_skill_hints
Revises: 066_derivation_queue_vault_fk
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '067_add_skill_hints'
down_revision: str | None = '066_derivation_queue_vault_fk'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'procedural_entries',
        sa.Column(
            'skill_hints',
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text('ARRAY[]::text[]'),
        ),
    )
    op.add_column(
        'procedural_entry_versions',
        sa.Column(
            'skill_hints',
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text('ARRAY[]::text[]'),
        ),
    )


def downgrade() -> None:
    op.drop_column('procedural_entries', 'skill_hints')
    op.drop_column('procedural_entry_versions', 'skill_hints')
