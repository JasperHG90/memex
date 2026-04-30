"""F14 procedure_outcomes — STUB; filled by WS-quick-wins.

Tier A seed stub — body intentionally raises so any feature workstream that
forgets to replace it before shipping fails CI loud. `alembic check` validates
the chain without invoking upgrade()/downgrade(), so this stub passes chain
integrity checks while remaining unrunnable.

Revision ID: 028_procedure_outcomes
Revises: 027_consolidation_ticks
Create Date: 2026-04-30
"""

from typing import Sequence, Union

revision: str = '028_procedure_outcomes'
down_revision: Union[str, None] = '027_consolidation_ticks'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    raise NotImplementedError('F14: filled in WS-quick-wins PR')


def downgrade() -> None:
    raise NotImplementedError('F14: filled in WS-quick-wins PR')
