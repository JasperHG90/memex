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

Embeddings & indexes: ``KVEntry.embedding`` is a column on the SAME row
as ``key`` and is generated from ``value`` (not from ``key``), so key
rewrites leave embeddings semantically valid and physically attached.
The ``idx_kv_key_prefix`` btree index updates automatically on row
UPDATE — no REINDEX required.

Revision ID: 046_procedure_to_global
Revises: 045_drop_procedure_outcomes
Create Date: 2026-05-29
"""

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger('alembic.046_procedure_to_global')

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

    # Match only well-formed 3-segment bare procedure keys
    # (`procedure:<verb>:<context>`) — i.e. exactly two colons after
    # `procedure:`. A multi-segment legacy key like `procedure:foo:bar:baz`
    # (4+ segments) shouldn't exist (the old validator rejected it) but
    # if it does via direct DB manipulation, rewriting it to
    # `global:procedure:foo:bar:baz` would create a row that the new
    # parser rejects — silently unreachable. Leave such rows for operator
    # triage instead.
    conn.execute(
        sa.text(
            'UPDATE kv_entries '
            "SET key = 'global:' || key, updated_at = now() "
            "WHERE key LIKE 'procedure:%:%' "
            "  AND key NOT LIKE 'procedure:%:%:%' "
            '  AND NOT EXISTS ('
            '    SELECT 1 FROM kv_entries inner_e '
            "    WHERE inner_e.key = 'global:' || kv_entries.key"
            '  )'
        )
    )

    # Surface any non-`global:procedure:*` scoped-procedure rows the
    # operator may want to triage. These cannot be touched automatically
    # — they belong to user/project/app scopes that have no pre-upgrade
    # bare equivalent — but they're worth flagging so a follow-up
    # downgrade doesn't leave silently-orphaned rows.
    #
    # Patterns use `:procedure:` infix with a `%` tail covering BOTH
    # `<verb>:<context>` segments (no extra `:` in the middle) so they
    # avoid matching unrelated keys like `user:procedure-settings`
    # (no `:procedure:` infix) or `project:foo:procedure_v2:bar`
    # (different infix). This is a diagnostic count; a stray match
    # is non-destructive — but tighter patterns make the orphan count
    # more meaningful to the operator.
    orphan_count = conn.execute(
        sa.text(
            'SELECT COUNT(*) FROM kv_entries '
            "WHERE key LIKE 'user:procedure:%:%' "
            "   OR key LIKE 'project:%:procedure:%:%' "
            "   OR key LIKE 'app:%:procedure:%:%'"
        )
    ).scalar()
    if orphan_count:
        # NOTE: these rows are VALID under the new schema — not orphans in
        # the usual sense. The variable name and log message preserve the
        # operator's mental model that the downgrade path will not reverse
        # them (so a future rollback would leave them in place). They are
        # working procedure keys today; the warning exists only to surface
        # the downgrade asymmetry, not to suggest they need fixing.
        logger.warning(
            'Migration 046 upgrade: %d scoped-procedure rows already exist '
            '(user:procedure:*, project:<id>:procedure:*, or '
            'app:<id>:procedure:*). These are kept as-is; downgrade will NOT '
            'reverse them. List them with: '
            "SELECT key FROM kv_entries WHERE key LIKE 'user:procedure:%%:%%' "
            "OR key LIKE 'project:%%:procedure:%%:%%' OR key LIKE 'app:%%:procedure:%%:%%';",
            orphan_count,
        )


def downgrade() -> None:
    # Asymmetric in two ways:
    #
    # 1. `global:procedure:*` rows are indiscriminately stripped back to
    #    bare `procedure:*` — this includes rows freshly created AFTER the
    #    upgrade, not just rows the upgrade migrated. The downgrade has no
    #    way to distinguish "migrated from bare" vs. "born scoped". For
    #    pure DB-rollback this is correct (the old code accepts bare). For
    #    DB-rollback WITHOUT code-rollback, the resulting bare keys are
    #    rejected by `_validate_namespace` on write — operators must roll
    #    back the application code together with this migration. There is
    #    no schema column to track migration provenance.
    #
    # 2. `user:procedure:*`, `project:<id>:procedure:*`, and
    #    `app:<id>:procedure:*` keys created after the upgrade are left
    #    in place — they have no pre-upgrade bare-form equivalent.
    #    Strict reversibility would require deleting them; that's
    #    destructive, so they're left for operator triage.
    logger.warning(
        'Migration 046 downgrade: strips global:procedure:<verb>:<context> -> '
        'procedure:<verb>:<context> (3-segment suffix only; the SQL pattern '
        'is `global:procedure:%%:%%`). Rows BORN scoped after the upgrade '
        'are stripped too. The bare form is only valid under pre-migration '
        'application code — roll back the application together with this '
        'migration. user:procedure:*, project:<id>:procedure:*, and '
        'app:<id>:procedure:* rows are left in place (no pre-upgrade equivalent).'
    )
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

    # Strip the leading 'global:' prefix from any global:procedure:* keys,
    # but only if a bare procedure:* row doesn't already exist for that
    # key. PostgreSQL substring(x from n) is 1-indexed and includes
    # position n; `char_length('global:') + 1` is the first position
    # AFTER the prefix, so the result is `procedure:...`.
    #
    # Pattern is `global:procedure:%:%` (not `global:procedure:%`) so we
    # only strip keys with the two-segment <verb>:<context> suffix.
    # Defends against operator-injected oddities like
    # `global:procedure:malformed_single_segment` getting silently
    # rewritten to a bare form the post-migration code can't accept.
    conn.execute(
        sa.text(
            'UPDATE kv_entries '
            "SET key = substring(key from char_length('global:') + 1), "
            '    updated_at = now() '
            "WHERE key LIKE 'global:procedure:%:%' "
            '  AND NOT EXISTS ('
            '    SELECT 1 FROM kv_entries inner_e '
            "    WHERE inner_e.key = substring(kv_entries.key from char_length('global:') + 1)"
            '  )'
        )
    )
