"""Add memory_units.claim_type column.

Adds an additive nullable column carrying the explicit-claim signal
('resolution' | 'contradiction' | NULL) extracted from text by the
LLM. Pre-existing rows remain NULL — the "no explicit claim" branch
in the contradiction engine treats NULL identically to legacy
behaviour.

No index: ``claim_type`` is read inside per-unit code paths
(``ContradictionEngine._process_flagged_unit`` and the new
``claim_too_aggressive`` lint), never used as a top-level query
filter.

Downgrade drops the CK constraint first, then the column.

Revision ID: 039_memory_unit_claim_type
Revises: 038_link_type_refines
Create Date: 2026-05-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '039_memory_unit_claim_type'
down_revision: Union[str, None] = '038_link_type_refines'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'memory_units',
        sa.Column('claim_type', sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        'ck_memory_units_claim_type',
        'memory_units',
        "claim_type IS NULL OR claim_type IN ('resolution', 'contradiction')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_memory_units_claim_type', 'memory_units', type_='check')
    op.drop_column('memory_units', 'claim_type')
