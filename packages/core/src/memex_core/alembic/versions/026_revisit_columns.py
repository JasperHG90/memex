"""F20 revisit_columns — STUB; filled by WS-revisit.

Tier A seed stub — body intentionally raises so any feature workstream that
forgets to replace it before shipping fails CI loud. `alembic check` validates
the chain without invoking upgrade()/downgrade(), so this stub passes chain
integrity checks while remaining unrunnable.

Revision ID: 026_revisit_columns
Revises: 025_maintenance_proposals
Create Date: 2026-04-30
"""

revision: str = '026_revisit_columns'
down_revision: str | None = '025_maintenance_proposals'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    raise NotImplementedError('F20: filled in WS-revisit PR')


def downgrade() -> None:
    raise NotImplementedError('F20: filled in WS-revisit PR')
