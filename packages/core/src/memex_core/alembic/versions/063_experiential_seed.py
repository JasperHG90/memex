"""Seed the hidden ``experiential`` system vault and backfill KV procedures (V7).

This migration is data-only. It does two things:

  1. **Seed a system vault** named ``experiential`` (kind='system',
     policy={hidden: true}). This is the dedicated home for V7
     experiential-plane content. Hidden system vaults are excluded from
     user-facing lists via ``VaultService.kind != 'system'`` filtering
     (V11 introduced that filter; see migration 060).
  2. **Backfill legacy KV procedures.** Any ``<scope>:procedure:*`` rows
     already in ``kv_entries`` (V7's precursor storage) are duplicated as
     draft ``experiential_entries`` rows so the search/briefing surface
     can find them. The original KV rows are kept (read-only) for
     backwards compatibility. Backfilled rows are stamped with
     ``origin='kv_backfill'`` and a sidecar table
     ``_migrated_kv_procedures_063`` records the (kv_key, entry_id) pair
     so the downgrade can find and remove them.

Both steps are idempotent: re-running ``alembic upgrade head`` after a
partial application is a no-op.

Downgrade removes the sidecar's derived rows and deletes the seeded
system vault. The original ``kv_entries`` rows are NOT deleted on
downgrade (they predate the V7 schema and are not owned by it).

Revision ID: 063_experiential_seed
Revises: 062_notes_role
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '063_experiential_seed'
down_revision: str | None = '062_notes_role'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EXPERIENTIAL_VAULT_NAME = 'experiential'
_SIDECAR_TABLE = '_migrated_kv_procedures_063'


def _vault_id_by_name(conn, name: str):
    return conn.execute(
        sa.text('SELECT id FROM vaults WHERE name = :name'), {'name': name}
    ).scalar()


def upgrade() -> None:
    conn = op.get_bind()

    # -----------------------------------------------------------------------
    # 1. Sidecar table — tracks (kv_key, entry_id) pairs so downgrade can
    #    find the derived rows.
    # -----------------------------------------------------------------------
    if not conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.tables '
            'WHERE table_schema = current_schema() AND table_name = :n)'
        ),
        {'n': _SIDECAR_TABLE},
    ).scalar():
        op.create_table(
            _SIDECAR_TABLE,
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('kv_key', sa.Text(), nullable=False, unique=True),
            sa.Column('entry_id', postgresql.UUID(), nullable=False),
            sa.Column(
                'created_at',
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text('now()'),
            ),
        )

    # -----------------------------------------------------------------------
    # 2. Seed the hidden experiential system vault.
    # -----------------------------------------------------------------------
    if _vault_id_by_name(conn, _EXPERIENTIAL_VAULT_NAME) is None:
        # jsonb is built via the ::jsonb cast in SQL (avoids the asyncpg
        # JSONB-cast double-encode gotcha — see MEMORY.md).
        conn.execute(
            sa.text(
                'INSERT INTO vaults (id, name, description, mw_mode, kind, policy) '
                "VALUES (gen_random_uuid(), :name, :desc, 'stationary', 'system', "
                '\'{"hidden": true}\'::jsonb)'
            ),
            {
                'name': _EXPERIENTIAL_VAULT_NAME,
                'desc': 'V7 procedural & experiential memory plane (system vault).',
            },
        )

    vault_id = _vault_id_by_name(conn, _EXPERIENTIAL_VAULT_NAME)
    assert vault_id is not None, 'experiential vault seed failed'

    # -----------------------------------------------------------------------
    # 3. Backfill KV procedures → draft experiential_entries.
    # -----------------------------------------------------------------------
    # A legacy procedure row lives at a key like ``<scope>:procedure:<verb>:<context>``
    # (or just ``<scope>:procedure:<verb>`` if context was absent). The scope
    # segment is everything before the first ``:procedure:`` marker. We
    # synthesise title from verb (with context as a parenthetical) and store
    # the value text as the body.
    legacy_rows = conn.execute(
        sa.text(
            "SELECT key, value FROM kv_entries WHERE key SIMILAR TO '%:procedure:[^:]+(:[^:]+)?'"
        )
    ).fetchall()

    for kv_key, kv_value in legacy_rows:
        # Skip if sidecar already records this row.
        already = conn.execute(
            sa.text(f'SELECT 1 FROM {_SIDECAR_TABLE} WHERE kv_key = :k'),
            {'k': kv_key},
        ).scalar()
        if already is not None:
            continue

        # Parse ``<scope>:procedure:<verb>:<context>`` — context is optional.
        # We split on ``:procedure:`` exactly once.
        marker = ':procedure:'
        marker_idx = kv_key.find(marker)
        if marker_idx < 0:
            continue
        scope = kv_key[:marker_idx] or 'global'
        rest = kv_key[marker_idx + len(marker) :]
        parts = rest.split(':', 1)
        verb = parts[0] or None
        context = parts[1] if len(parts) > 1 else None

        title = verb or 'procedure'
        if context:
            title = f'{title} ({context})'

        # NOTE: jsonb is constructed inline via the `::jsonb` cast (avoids
        # the asyncpg JSONB-cast double-encode gotcha — see MEMORY.md).
        # The kv_key is escaped to keep the cast safe; the regex constraint
        # on kv_keys already excludes characters that would break JSON.
        kv_key_json = kv_key.replace('\\', '\\\\').replace('"', '\\"')
        entry_id_row = conn.execute(
            sa.text(
                """
                INSERT INTO experiential_entries
                    (vault_id, kind, scope, verb, context, title, summary, body,
                     status, origin, tags, metadata)
                VALUES
                    (:vault_id, 'procedure', :scope, :verb, :context, :title,
                     :summary, :body, 'draft', 'kv_backfill',
                     ARRAY[]::text[], (:metadata_json)::jsonb)
                RETURNING id
                """
            ),
            {
                'vault_id': vault_id,
                'scope': scope,
                'verb': verb,
                'context': context,
                'title': title,
                'summary': f'Backfilled from KV key {kv_key!r} (V6 legacy).',
                'body': kv_value or '',
                'metadata_json': f'{{"kv_source_key": "{kv_key_json}"}}',
            },
        ).first()
        entry_id = entry_id_row[0]

        conn.execute(
            sa.text(f'INSERT INTO {_SIDECAR_TABLE} (kv_key, entry_id) VALUES (:k, :e)'),
            {'k': kv_key, 'e': entry_id},
        )


def downgrade() -> None:
    conn = op.get_bind()

    # 1. Drop the derived experiential_entries (any row recorded in the
    #    sidecar) — original KV rows are untouched.
    derived_ids = [
        row[0] for row in conn.execute(sa.text(f'SELECT entry_id FROM {_SIDECAR_TABLE}')).fetchall()
    ]
    if derived_ids:
        conn.execute(
            sa.text('DELETE FROM experiential_entries WHERE id = ANY(CAST(:ids AS uuid[]))'),
            {'ids': derived_ids},
        )

    # 2. Drop the sidecar.
    if conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.tables '
            'WHERE table_schema = current_schema() AND table_name = :n)'
        ),
        {'n': _SIDECAR_TABLE},
    ).scalar():
        op.drop_table(_SIDECAR_TABLE)

    # 3. Delete the seeded vault. (Other rows in `experiential_entries` may
    #    reference it via FK CASCADE — we delete those too via FK CASCADE.
    #    Operators who have published experiential content SHOULD NOT
    #    downgrade.)
    conn.execute(
        sa.text('DELETE FROM vaults WHERE name = :n'),
        {'n': _EXPERIENTIAL_VAULT_NAME},
    )
