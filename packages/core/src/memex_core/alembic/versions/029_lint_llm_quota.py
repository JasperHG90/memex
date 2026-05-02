"""F10 lint_llm_quota — STUB; filled by WS-linter.

Tier A seed stub — body intentionally raises so any feature workstream that
forgets to replace it before shipping fails CI loud. `alembic check` validates
the chain without invoking upgrade()/downgrade(), so this stub passes chain
integrity checks while remaining unrunnable.

Revision ID: 029_lint_llm_quota
Revises: 028_procedure_outcomes
Create Date: 2026-04-30
"""

revision: str = '029_lint_llm_quota'
down_revision: str | None = '028_procedure_outcomes'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    raise NotImplementedError('F10: filled in WS-linter PR')


def downgrade() -> None:
    raise NotImplementedError('F10: filled in WS-linter PR')
