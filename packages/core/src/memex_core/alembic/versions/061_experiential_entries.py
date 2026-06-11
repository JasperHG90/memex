"""experiential_entries + 4 sibling tables (procedural memory plane).

Adds the experiential plane schema:

  * ``experiential_entries``           — case | procedure | strategy rows
  * ``experiential_entry_versions``    — append-only version ledger
  * ``experiential_sources``           — provenance / evidence / contradiction edges
  * ``experiential_pins``              — context-binding pin chain
  * ``experiential_derivation_queue``  — async case → procedure/strategy queue

Identity for procedures and strategies is the (kind, scope, verb, context)
tuple, made unique via ``UNIQUE NULLS NOT DISTINCT`` (so a case's NULL
verb/context does not collide with a procedure's). Cases use a free-form
*trigger* phrase + freshly-computed embedding (recomputed on every
trigger change to avoid stale-vector drift, per spike 7).

HNSW indexes on both ``body_embedding`` (procedures/strategies) and
``trigger_embedding`` (cases) use ``vector_cosine_ops``. A generated
tsvector column ``search_tsvector`` powers BM25 keyword search via
GIN — mirroring the ``memory_units.search_tsvector`` pattern from
migration 009.

Downgrade drops tables and indexes; data loss is intentional (the
tables hold experiential-plane state, not declarative data). Each
table's existence is guarded with ``CREATE TABLE IF NOT EXISTS`` so
re-running upgrade on a partially-applied schema is safe.

Revision ID: 061_experiential_entries
Revises: 060_vault_kind_policy
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '061_experiential_entries'
down_revision: str | None = '060_vault_kind_policy'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SEARCH_TSVECTOR_EXPR = (
    "to_tsvector('english'::regconfig, "
    "coalesce(title, '') || ' ' || "
    "coalesce(summary, '') || ' ' || "
    "coalesce(body, '') || ' ' || "
    "coalesce(memex_experiential_tags_to_text(tags), ''))"
)


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text(
                'SELECT EXISTS ('
                '  SELECT 1 FROM information_schema.tables '
                '  WHERE table_schema = current_schema() '
                '    AND table_name = :name'
                ')'
            ),
            {'name': table_name},
        ).scalar()
    )


def _index_exists(index_name: str) -> bool:
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text('SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :name)'),
            {'name': index_name},
        ).scalar()
    )


def _function_exists(function_name: str) -> bool:
    conn = op.get_bind()
    return bool(
        conn.execute(
            sa.text('SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = :name)'),
            {'name': function_name},
        ).scalar()
    )


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 0. memex_experiential_tags_to_text — IMMUTABLE wrapper around
    #    array_to_string. Postgres marks array_to_string as STABLE (collation
    #    sensitive) which disqualifies it from generated-column expressions.
    #    This wrapper hard-codes an empty-element behaviour (NULL → '')
    #    so the function body is provably immutable.
    #
    #    SQLModel.metadata.create_all (used by integration tests) also emits
    #    this function via a 'before_create' DDL event in sql_models.py —
    #    see _emit_experiential_tags_to_text. Both paths use the same
    #    CREATE OR REPLACE body so they're idempotent against each other.
    # -------------------------------------------------------------------------
    if not _function_exists('memex_experiential_tags_to_text'):
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION memex_experiential_tags_to_text(
                    tags text[]
                ) RETURNS text
                LANGUAGE sql
                IMMUTABLE
                PARALLEL SAFE
                AS $$
                    SELECT coalesce(
                        array_to_string(tags, ' '),
                        ''
                    )
                $$
                """
            )
        )

    # -------------------------------------------------------------------------
    # 1. experiential_entries
    # -------------------------------------------------------------------------
    if not _table_exists('experiential_entries'):
        op.execute(
            sa.text(
                """
                CREATE TABLE experiential_entries (
                    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    vault_id      uuid NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                    kind          text NOT NULL,
                    scope         text NOT NULL,
                    verb          text,
                    context       text,
                    title         text NOT NULL,
                    summary       text NOT NULL,
                    body          text NOT NULL DEFAULT '',
                    trigger       text,
                    trigger_embedding vector(384),
                    tags          text[] NOT NULL DEFAULT ARRAY[]::text[],
                    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
                    status        text NOT NULL DEFAULT 'draft',
                    origin        text NOT NULL DEFAULT 'manual',
                    supersedes_id     uuid,
                    superseded_by_id  uuid,
                    body_embedding vector(384),
                    search_tsvector tsvector
                        GENERATED ALWAYS AS ("""
                + _SEARCH_TSVECTOR_EXPR
                + """) STORED,
                    created_at    timestamptz NOT NULL DEFAULT now(),
                    updated_at    timestamptz NOT NULL DEFAULT now(),
                    published_at  timestamptz
                )
                """
            )
        )

        op.execute(
            sa.text(
                """
                ALTER TABLE experiential_entries
                ADD CONSTRAINT uq_experiential_identity
                    UNIQUE NULLS NOT DISTINCT (kind, scope, verb, context)
                """
            )
        )
        op.execute(
            sa.text(
                'ALTER TABLE experiential_entries '
                'ADD CONSTRAINT ck_experiential_kind '
                "CHECK (kind IN ('case', 'procedure', 'strategy'))"
            )
        )
        op.execute(
            sa.text(
                'ALTER TABLE experiential_entries '
                'ADD CONSTRAINT ck_experiential_status '
                "CHECK (status IN ('draft', 'published', 'deprecated'))"
            )
        )
        op.execute(
            sa.text(
                'ALTER TABLE experiential_entries '
                'ADD CONSTRAINT ck_experiential_origin '
                "CHECK (origin IN ('seed', 'derived', 'authored', 'manual', 'import'))"
            )
        )
        op.execute(
            sa.text(
                'ALTER TABLE experiential_entries '
                'ADD CONSTRAINT ck_strategy_context '
                "CHECK (kind <> 'strategy' OR (verb IS NOT NULL AND context IS NOT NULL))"
            )
        )
        # NOTE: there is no `ck_experiential_trigger_paired` CHECK. The
        # repository never writes `trigger_embedding` on create/update
        # (lazy-embedding design — see experiential_repository.py
        # module docstring). A CHECK requiring
        # `(trigger IS NULL) = (trigger_embedding IS NULL)` would fire on
        # every case create that supplies a trigger, surfacing a
        # `CheckViolationError` that the repository would mis-translate
        # as `ExperientialIdentityConflict` (409). The intent of the
        # pairing — "a trigger must have a corresponding embedding" — is
        # enforced at the search-service layer, where the back-fill
        # worker promotes a `trigger IS NOT NULL` row to having a
        # `trigger_embedding`.
        op.execute(
            sa.text(
                'ALTER TABLE experiential_entries '
                'ADD CONSTRAINT ck_experiential_body_embedding_scope '
                "CHECK ((body_embedding IS NULL) OR (kind IN ('procedure', 'strategy')))"
            )
        )
        op.execute(
            sa.text(
                'ALTER TABLE experiential_entries '
                'ADD CONSTRAINT experiential_entries_supersedes_fkey '
                'FOREIGN KEY (supersedes_id) REFERENCES experiential_entries(id) '
                'ON DELETE SET NULL'
            )
        )
        op.execute(
            sa.text(
                'ALTER TABLE experiential_entries '
                'ADD CONSTRAINT experiential_entries_superseded_by_fkey '
                'FOREIGN KEY (superseded_by_id) REFERENCES experiential_entries(id) '
                'ON DELETE SET NULL'
            )
        )

        if not _index_exists('idx_experiential_entries_vault_kind'):
            op.execute(
                sa.text(
                    'CREATE INDEX idx_experiential_entries_vault_kind '
                    'ON experiential_entries (vault_id, kind)'
                )
            )
        if not _index_exists('idx_experiential_entries_vault_status'):
            op.execute(
                sa.text(
                    'CREATE INDEX idx_experiential_entries_vault_status '
                    'ON experiential_entries (vault_id, status)'
                )
            )
        if not _index_exists('idx_experiential_entries_scope_verb'):
            op.execute(
                sa.text(
                    'CREATE INDEX idx_experiential_entries_scope_verb '
                    'ON experiential_entries (scope, verb) '
                    "WHERE kind IN ('procedure', 'strategy')"
                )
            )
        if not _index_exists('idx_experiential_entries_status_published_at'):
            op.execute(
                sa.text(
                    'CREATE INDEX idx_experiential_entries_status_published_at '
                    'ON experiential_entries (status, published_at DESC) '
                    "WHERE status = 'published'"
                )
            )
        if not _index_exists('idx_experiential_entries_body_embedding'):
            op.execute(
                sa.text(
                    'CREATE INDEX idx_experiential_entries_body_embedding '
                    'ON experiential_entries USING hnsw (body_embedding vector_cosine_ops) '
                    "WHERE status = 'published' AND kind IN ('procedure', 'strategy')"
                )
            )
        if not _index_exists('idx_experiential_entries_trigger_embedding'):
            op.execute(
                sa.text(
                    'CREATE INDEX idx_experiential_entries_trigger_embedding '
                    'ON experiential_entries USING hnsw (trigger_embedding vector_cosine_ops) '
                    "WHERE status = 'published' AND kind = 'case'"
                )
            )
        if not _index_exists('idx_experiential_entries_search_tsvector'):
            op.execute(
                sa.text(
                    'CREATE INDEX idx_experiential_entries_search_tsvector '
                    'ON experiential_entries USING gin (search_tsvector)'
                )
            )

    # -------------------------------------------------------------------------
    # 2. experiential_entry_versions — append-only version ledger
    # -------------------------------------------------------------------------
    if not _table_exists('experiential_entry_versions'):
        op.create_table(
            'experiential_entry_versions',
            sa.Column(
                'id',
                postgresql.UUID(),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column('entry_id', postgresql.UUID(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('title', sa.Text(), nullable=False),
            sa.Column('summary', sa.Text(), nullable=False),
            sa.Column('body', sa.Text(), nullable=False, server_default=''),
            sa.Column('trigger', sa.Text(), nullable=True),
            sa.Column(
                'tags',
                postgresql.ARRAY(sa.Text()),
                nullable=False,
                server_default=sa.text('ARRAY[]::text[]'),
            ),
            sa.Column(
                'metadata',
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column('edited_by', sa.Text(), nullable=True),
            sa.Column('edit_reason', sa.Text(), nullable=True),
            sa.Column(
                'created_at',
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text('now()'),
            ),
        )
        op.create_foreign_key(
            'experiential_entry_versions_entry_fkey',
            'experiential_entry_versions',
            'experiential_entries',
            ['entry_id'],
            ['id'],
            ondelete='CASCADE',
        )
        op.create_unique_constraint(
            'uq_experiential_entry_versions_entry_version',
            'experiential_entry_versions',
            ['entry_id', 'version'],
        )
        op.create_index(
            'idx_experiential_entry_versions_entry_id_created_at',
            'experiential_entry_versions',
            ['entry_id', 'created_at'],
        )

    # -------------------------------------------------------------------------
    # 3. experiential_sources — provenance/evidence/contradiction edges
    # -------------------------------------------------------------------------
    if not _table_exists('experiential_sources'):
        op.create_table(
            'experiential_sources',
            sa.Column(
                'id',
                postgresql.UUID(),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column('entry_id', postgresql.UUID(), nullable=False),
            sa.Column('source_entry_id', postgresql.UUID(), nullable=True),
            sa.Column('source_note_id', postgresql.UUID(), nullable=True),
            sa.Column('source_memory_unit_id', postgresql.UUID(), nullable=True),
            sa.Column('role', sa.String(), nullable=False),
            sa.Column('weight', sa.Float(), nullable=False, server_default='1.0'),
            sa.Column(
                'created_at',
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text('now()'),
            ),
        )
        op.create_check_constraint(
            'ck_experiential_sources_role',
            'experiential_sources',
            "role IN ('provenance', 'evidence', 'contradiction')",
        )
        op.create_check_constraint(
            'ck_experiential_sources_weight',
            'experiential_sources',
            'weight >= 0.0 AND weight <= 10.0',
        )
        op.create_check_constraint(
            'ck_experiential_sources_pointer_set',
            'experiential_sources',
            'source_entry_id IS NOT NULL OR source_note_id IS NOT NULL OR '
            'source_memory_unit_id IS NOT NULL',
        )
        op.create_foreign_key(
            'experiential_sources_entry_fkey',
            'experiential_sources',
            'experiential_entries',
            ['entry_id'],
            ['id'],
            ondelete='CASCADE',
        )
        op.create_foreign_key(
            'experiential_sources_source_entry_fkey',
            'experiential_sources',
            'experiential_entries',
            ['source_entry_id'],
            ['id'],
            ondelete='CASCADE',
        )
        op.create_foreign_key(
            'experiential_sources_source_note_fkey',
            'experiential_sources',
            'notes',
            ['source_note_id'],
            ['id'],
            ondelete='SET NULL',
        )
        op.create_foreign_key(
            'experiential_sources_source_memory_unit_fkey',
            'experiential_sources',
            'memory_units',
            ['source_memory_unit_id'],
            ['id'],
            ondelete='SET NULL',
        )
        op.create_index('idx_experiential_sources_entry_id', 'experiential_sources', ['entry_id'])
        op.create_index(
            'idx_experiential_sources_source_entry_id',
            'experiential_sources',
            ['source_entry_id'],
        )

    # -------------------------------------------------------------------------
    # 4. experiential_pins — context-binding pin chain (spike 7)
    # -------------------------------------------------------------------------
    if not _table_exists('experiential_pins'):
        op.create_table(
            'experiential_pins',
            sa.Column(
                'id',
                postgresql.UUID(),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column('context_key', sa.Text(), nullable=False),
            sa.Column('entry_id', postgresql.UUID(), nullable=False),
            sa.Column('position', sa.Integer(), nullable=False),
            sa.Column('pinned_by', sa.Text(), nullable=True),
            sa.Column(
                'created_at',
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text('now()'),
            ),
        )
        op.create_foreign_key(
            'experiential_pins_entry_fkey',
            'experiential_pins',
            'experiential_entries',
            ['entry_id'],
            ['id'],
            ondelete='CASCADE',
        )
        op.create_unique_constraint(
            'uq_experiential_pins_chain_position',
            'experiential_pins',
            ['context_key', 'entry_id', 'position'],
        )
        op.create_check_constraint(
            'ck_experiential_pins_position_nonneg',
            'experiential_pins',
            'position >= 0',
        )
        op.create_index(
            'idx_experiential_pins_context_position',
            'experiential_pins',
            ['context_key', 'position'],
        )

    # -------------------------------------------------------------------------
    # 5. experiential_derivation_queue — async case → procedure/strategy
    # -------------------------------------------------------------------------
    if not _table_exists('experiential_derivation_queue'):
        op.create_table(
            'experiential_derivation_queue',
            sa.Column(
                'id',
                postgresql.UUID(),
                primary_key=True,
                server_default=sa.text('gen_random_uuid()'),
            ),
            sa.Column('vault_id', postgresql.UUID(), nullable=False),
            sa.Column(
                'source_entry_ids',
                postgresql.ARRAY(postgresql.UUID()),
                nullable=False,
                server_default=sa.text('ARRAY[]::uuid[]'),
            ),
            sa.Column('target_kind', sa.String(), nullable=False),
            sa.Column('target_scope', sa.Text(), nullable=False),
            sa.Column('target_verb', sa.Text(), nullable=True),
            sa.Column('target_context', sa.Text(), nullable=True),
            sa.Column(
                'status',
                sa.String(),
                nullable=False,
                server_default='pending',
            ),
            sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('result_entry_id', postgresql.UUID(), nullable=True),
            sa.Column('claimed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
            sa.Column('completed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
            sa.Column(
                'created_at',
                postgresql.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text('now()'),
            ),
        )
        op.create_check_constraint(
            'ck_derivation_queue_target_kind',
            'experiential_derivation_queue',
            "target_kind IN ('procedure', 'strategy')",
        )
        op.create_check_constraint(
            'ck_derivation_queue_status',
            'experiential_derivation_queue',
            "status IN ('pending', 'in_progress', 'completed', 'failed')",
        )
        op.create_check_constraint(
            'ck_derivation_queue_attempt_nonneg',
            'experiential_derivation_queue',
            'attempt_count >= 0',
        )
        op.create_check_constraint(
            'ck_derivation_queue_strategy_context',
            'experiential_derivation_queue',
            "target_kind <> 'strategy' OR (target_verb IS NOT NULL AND target_context IS NOT NULL)",
        )
        op.create_index(
            'idx_derivation_queue_status_created_at',
            'experiential_derivation_queue',
            ['status', 'created_at'],
            postgresql_where=sa.text("status = 'pending'"),
        )
        op.create_index(
            'idx_derivation_queue_vault_id',
            'experiential_derivation_queue',
            ['vault_id'],
        )


def downgrade() -> None:
    # Order: dependents first.
    for table in (
        'experiential_derivation_queue',
        'experiential_pins',
        'experiential_sources',
        'experiential_entry_versions',
        'experiential_entries',
    ):
        if _table_exists(table):
            op.drop_table(table)

    if _function_exists('memex_experiential_tags_to_text'):
        op.execute(sa.text('DROP FUNCTION memex_experiential_tags_to_text(text[])'))
