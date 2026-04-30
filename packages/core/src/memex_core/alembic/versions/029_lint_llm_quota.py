"""F10 lint_llm_quota — STUB; filled by WS-linter.

Tier A seed stub — body intentionally raises so any feature workstream that
forgets to replace it before shipping fails CI loud. `alembic check` validates
the chain without invoking upgrade()/downgrade(), so this stub passes chain
integrity checks while remaining unrunnable.

Revision ID: 029_lint_llm_quota
Revises: 028_procedure_outcomes
Create Date: 2026-04-30
"""

from typing import Sequence, Union

revision: str = '029_lint_llm_quota'
down_revision: Union[str, None] = '028_procedure_outcomes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    raise NotImplementedError('F10: filled in WS-linter PR')


def downgrade() -> None:
    raise NotImplementedError('F10: filled in WS-linter PR')
