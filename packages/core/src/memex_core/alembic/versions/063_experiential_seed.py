"""Seed the hidden ``experiential`` system vault.

This migration is data-only. It seeds a single system vault named
``experiential`` (kind='system', policy={reflect: true} — the §18.9.0
override: reflection ON, vault-summary OFF). This is the dedicated home
for procedural-plane content (cases). Hidden system vaults are excluded
from user-facing lists via ``VaultService.kind != 'system'`` filtering
(see migration 060).

Procedures/strategies are NOT migrated from KV — KV never owned them in
this design line, and the legacy ``<scope>:procedure:*`` KV namespace has
been removed entirely (it is not a backfill source).

The step is idempotent: re-running ``alembic upgrade head`` after a
partial application is a no-op.

Downgrade deletes the seeded system vault (rows in ``experiential_entries``
referencing it go via FK CASCADE — operators with published procedural
content SHOULD NOT downgrade).

Revision ID: 063_experiential_seed
Revises: 062_notes_role
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '063_experiential_seed'
down_revision: str | None = '062_notes_role'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EXPERIENTIAL_VAULT_NAME = 'experiential'


def _vault_id_by_name(conn, name: str):
    return conn.execute(
        sa.text('SELECT id FROM vaults WHERE name = :name'), {'name': name}
    ).scalar()


def upgrade() -> None:
    conn = op.get_bind()

    # -----------------------------------------------------------------------
    # Seed the hidden experiential system vault.
    #
    # policy = {"reflect": false}: reflection OFF, summary OFF (both the
    # system-vault default). §18.9.0 originally specced reflection ON, but
    # the derivation pipeline reads cases directly — it never consumes the
    # per-entity mental models reflection produces — so reflecting over the
    # hidden case vault is wasted LLM compute. The blob MUST validate
    # against the typed VaultPolicy (extra='forbid') — an unknown key like
    # "hidden" raises ValidationError in coerce_policy() on every
    # reflect_enabled/summarize_enabled call that touches this vault, which
    # would break case-ingest extraction AND abort the vault-summary sweep.
    # Hiding is driven by kind='system', not a policy flag.
    # -----------------------------------------------------------------------
    if _vault_id_by_name(conn, _EXPERIENTIAL_VAULT_NAME) is None:
        # jsonb is built via the ::jsonb cast in SQL (avoids the asyncpg
        # JSONB-cast double-encode gotcha — see MEMORY.md).
        conn.execute(
            sa.text(
                'INSERT INTO vaults (id, name, description, mw_mode, kind, policy) '
                "VALUES (gen_random_uuid(), :name, :desc, 'stationary', 'system', "
                '\'{"reflect": false}\'::jsonb)'
            ),
            {
                'name': _EXPERIENTIAL_VAULT_NAME,
                'desc': 'procedural & experiential memory plane (system vault).',
            },
        )

    vault_id = _vault_id_by_name(conn, _EXPERIENTIAL_VAULT_NAME)
    assert vault_id is not None, 'experiential vault seed failed'


def downgrade() -> None:
    conn = op.get_bind()

    # Delete the seeded vault. Rows in ``experiential_entries`` referencing
    # it go via FK CASCADE — operators with published procedural content
    # SHOULD NOT downgrade.
    conn.execute(
        sa.text('DELETE FROM vaults WHERE name = :n'),
        {'n': _EXPERIENTIAL_VAULT_NAME},
    )
