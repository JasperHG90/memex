"""Add vault_id to entity_cooccurrences primary key + rebuild from ground truth.

The original PK was ``(entity_id_1, entity_id_2)``, with ``vault_id`` as a
non-key column. Because cooccurrence is tracked per-vault and read back with a
``WHERE vault_id = ?`` filter (retrieval graph strategies, the
``get_entity_cooccurrences`` API, snapshot export), a single entity pair that
appears in two vaults could only ever occupy ONE row. The ingest upsert
(``ON CONFLICT (entity_id_1, entity_id_2) DO UPDATE``) then summed counts across
vaults into that one row and left ``vault_id`` pointing at whichever vault
observed the pair first. Result: the second vault's edge was invisible to
retrieval, and the first vault's edge was inflated.

Fix: make the grain ``(entity_id_1, entity_id_2, vault_id)``. Existing rows are
already corrupt (cross-vault summed counts, arbitrary vault_id), so they cannot
be split back — the table is rebuilt from ground truth (``unit_entities`` joined
to ``memory_units``). The rebuilt count is the number of distinct units in the
vault that co-mention the pair, matching the ingest-time semantics.

Operational note: the rebuild is a single ``unit_entities`` self-join over the
whole table. On a very large corpus this is a heavy one-shot statement that
holds a lock on ``entity_cooccurrences`` for its duration — run it in a
maintenance window on big deployments.

Revision ID: 052_entity_cooccurrence_vault_pk
Revises: 051_fix_telemetry_pk
Create Date: 2026-05-29
"""

from alembic import op

revision: str = '052_entity_cooccurrence_vault_pk'
down_revision: str | None = '051_fix_telemetry_pk'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


# Rebuild from ground truth: one row per (pair, vault); count = distinct units in
# that vault co-mentioning the pair; valid_from = earliest unit event_date.
# Canonical ordering (entity_id_1 < entity_id_2) is enforced by the self-join,
# matching the table's CHECK constraint.
_REBUILD_PER_VAULT = """
    INSERT INTO entity_cooccurrences
        (entity_id_1, entity_id_2, vault_id, cooccurrence_count, last_cooccurred, valid_from)
    SELECT a.entity_id, b.entity_id, mu.vault_id,
           COUNT(DISTINCT a.unit_id), now(), MIN(mu.event_date)
    FROM unit_entities a
    JOIN unit_entities b ON a.unit_id = b.unit_id AND a.entity_id < b.entity_id
    JOIN memory_units mu ON mu.id = a.unit_id
    GROUP BY a.entity_id, b.entity_id, mu.vault_id
"""

# Downgrade rebuild: collapse back to one row per pair (the old, lossy grain).
# Count sums across vaults; vault_id is arbitrary (earliest by uuid order).
_REBUILD_GLOBAL = """
    INSERT INTO entity_cooccurrences
        (entity_id_1, entity_id_2, vault_id, cooccurrence_count, last_cooccurred, valid_from)
    SELECT a.entity_id, b.entity_id, MIN(mu.vault_id::text)::uuid,
           COUNT(DISTINCT a.unit_id), now(), MIN(mu.event_date)
    FROM unit_entities a
    JOIN unit_entities b ON a.unit_id = b.unit_id AND a.entity_id < b.entity_id
    JOIN memory_units mu ON mu.id = a.unit_id
    GROUP BY a.entity_id, b.entity_id
"""


def upgrade() -> None:
    op.drop_constraint('entity_cooccurrences_pkey', 'entity_cooccurrences', type_='primary')
    # Existing rows carry cross-vault-summed counts that cannot be split — discard
    # and rebuild from ground truth under the new grain.
    op.execute('TRUNCATE entity_cooccurrences')
    op.create_primary_key(
        'entity_cooccurrences_pkey',
        'entity_cooccurrences',
        ['entity_id_1', 'entity_id_2', 'vault_id'],
    )
    op.execute(_REBUILD_PER_VAULT)


def downgrade() -> None:
    op.drop_constraint('entity_cooccurrences_pkey', 'entity_cooccurrences', type_='primary')
    op.execute('TRUNCATE entity_cooccurrences')
    op.create_primary_key(
        'entity_cooccurrences_pkey',
        'entity_cooccurrences',
        ['entity_id_1', 'entity_id_2'],
    )
    op.execute(_REBUILD_GLOBAL)
