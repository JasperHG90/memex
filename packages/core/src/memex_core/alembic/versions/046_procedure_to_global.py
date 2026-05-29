"""Rename bare procedure:* KV keys to global:procedure:*.

Procedures are no longer a top-level KV namespace. They now live UNDER
an existing scope namespace as ``<scope>:procedure:<verb>:<context>``.
This migration rewrites legacy ``procedure:<verb>:<context>`` keys in
``kv_entries`` to the new ``global:procedure:<verb>:<context>`` form so
they remain discoverable by ``is_procedure_key`` and continue to render
in the briefing's Procedures section.

Idempotent: the UPDATE only fires for rows still on the bare form, and
only when no row with the target ``global:`` form already exists for
that key (avoids unique-constraint violations when a manual rewrite
already happened).

Revision ID: 046_procedure_to_global
Revises: 045_drop_procedure_outcomes
Create Date: 2026-05-29
"""

import sqlalchemy as sa
from alembic import op

revision: str = '046_procedure_to_global'
down_revision: str | None = '045_drop_procedure_outcomes'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    # Skip silently if kv_entries doesn't exist yet (fresh DB before
    # baseline runs).
    table_exists = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.tables'
            "  WHERE table_schema = current_schema() AND table_name = 'kv_entries'"
            ')'
        )
    ).scalar()
    if not table_exists:
        return

    conn.execute(
        sa.text(
            'UPDATE kv_entries '
            "SET key = 'global:' || key, updated_at = now() "
            "WHERE key LIKE 'procedure:%' "
            '  AND NOT EXISTS ('
            '    SELECT 1 FROM kv_entries inner_e '
            "    WHERE inner_e.key = 'global:' || kv_entries.key"
            '  )'
        )
    )


def downgrade() -> None:
    # Asymmetric: upgrade rewrites bare `procedure:*` → `global:procedure:*`,
    # but only `global:procedure:*` is stripped back here. Any
    # `user:procedure:*`, `project:<id>:procedure:*`, or `app:<id>:procedure:*`
    # keys created AFTER the upgrade (these scopes are newly introduced
    # in this PR) are left in place — they have no pre-upgrade bare-form
    # equivalent. Strict reversibility would require deleting them, but
    # that's destructive; leave the orphans for the operator to triage.
    conn = op.get_bind()
    table_exists = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.tables'
            "  WHERE table_schema = current_schema() AND table_name = 'kv_entries'"
            ')'
        )
    ).scalar()
    if not table_exists:
        return

    # Strip the leading 'global:' from any global:procedure:* keys, but only
    # if a bare procedure:* row doesn't already exist for that key.
    conn.execute(
        sa.text(
            'UPDATE kv_entries '
            'SET key = substring(key from 8), updated_at = now() '
            "WHERE key LIKE 'global:procedure:%' "
            '  AND NOT EXISTS ('
            '    SELECT 1 FROM kv_entries inner_e '
            '    WHERE inner_e.key = substring(kv_entries.key from 8)'
            '  )'
        )
    )
