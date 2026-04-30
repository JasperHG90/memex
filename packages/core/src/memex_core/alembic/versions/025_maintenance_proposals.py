"""F6 maintenance_proposals — STUB; filled by WS-linter.

Tier A seed stub — body intentionally raises so any feature workstream that
forgets to replace it before shipping fails CI loud. `alembic check` validates
the chain without invoking upgrade()/downgrade(), so this stub passes chain
integrity checks while remaining unrunnable.

Revision ID: 025_maintenance_proposals
Revises: 024_intent_risk_classifier
Create Date: 2026-04-30
"""

from typing import Sequence, Union

revision: str = '025_maintenance_proposals'
down_revision: Union[str, None] = '024_intent_risk_classifier'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    raise NotImplementedError('F6: filled in WS-linter PR')


def downgrade() -> None:
    raise NotImplementedError('F6: filled in WS-linter PR')
