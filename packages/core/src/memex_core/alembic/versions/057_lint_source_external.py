"""Widen maintenance_proposals.source to accept externally-submitted proposals.

External tools (agent skills, routing agents) submit lint proposals through
``POST /api/v1/lint/proposals``; those rows carry ``source = 'external'`` so
the cockpit and the agent surface can distinguish them from SQL-rule and
LLM-check findings. Purely additive — no new columns or tables; the rule
metadata and the proposed action ride the existing ``evidence`` JSONB.

Revision ID: 057_lint_source_external
Revises: 056_node_assets
Create Date: 2026-06-04
"""

import logging

import sqlalchemy as sa
from alembic import op

revision: str = '057_lint_source_external'
down_revision: str | None = '056_node_assets'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


_CHECK_WITH_EXTERNAL = (
    'ALTER TABLE maintenance_proposals '
    'DROP CONSTRAINT IF EXISTS ck_maintenance_proposals_source; '
    'ALTER TABLE maintenance_proposals '
    'ADD CONSTRAINT ck_maintenance_proposals_source '
    "CHECK (source IN ('rule', 'llm', 'external'));"
)

_CHECK_WITHOUT_EXTERNAL = (
    'ALTER TABLE maintenance_proposals '
    'DROP CONSTRAINT IF EXISTS ck_maintenance_proposals_source; '
    'ALTER TABLE maintenance_proposals '
    'ADD CONSTRAINT ck_maintenance_proposals_source '
    "CHECK (source IN ('rule', 'llm'));"
)


def _exec_each(sql: str) -> None:
    """Execute a ``;``-separated SQL block one statement at a time.

    asyncpg sends every statement as a prepared statement and Postgres permits
    only one command per prepared statement, so multi-statement strings must be
    split (same contract as migration 055).
    """
    for statement in (s.strip() for s in sql.split(';')):
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_each(_CHECK_WITH_EXTERNAL)


def downgrade() -> None:
    # DATA LOSS: permanently deletes every externally-submitted proposal
    # (source='external'), including resolved ones whose catalogue actions
    # already ran — their audit trail is destroyed. This is the additive-
    # reverse of upgrade() (which only widened the CHECK to admit 'external');
    # an operator downgrading past this revision accepts the loss. The deleted
    # count is logged so the destruction is auditable.
    deleted = (
        op.get_bind()
        .execute(sa.text("DELETE FROM maintenance_proposals WHERE source = 'external'"))
        .rowcount
    )
    logging.getLogger('alembic.runtime.migration').warning(
        'downgrade 057_lint_source_external: deleted %d external maintenance_proposals row(s)',
        deleted,
    )
    _exec_each(_CHECK_WITHOUT_EXTERNAL)
