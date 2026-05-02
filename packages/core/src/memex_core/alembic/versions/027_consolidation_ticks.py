"""F38 consolidation_ticks — STUB; filled by WS-quick-wins.

Tier A seed stub — body intentionally raises so any feature workstream that
forgets to replace it before shipping fails CI loud. `alembic check` validates
the chain without invoking upgrade()/downgrade(), so this stub passes chain
integrity checks while remaining unrunnable.

Revision ID: 027_consolidation_ticks
Revises: 026_revisit_columns
Create Date: 2026-04-30
"""

revision: str = '027_consolidation_ticks'
down_revision: str | None = '026_revisit_columns'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    raise NotImplementedError('F38: filled in WS-quick-wins PR')


def downgrade() -> None:
    raise NotImplementedError('F38: filled in WS-quick-wins PR')
