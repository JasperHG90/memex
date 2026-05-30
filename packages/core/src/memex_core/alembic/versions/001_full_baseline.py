"""Full baseline: extensions + all 18 tables.

Revision ID: 001_full_baseline
Revises:
Create Date: 2026-02-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, ARRAY
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = '001_full_baseline'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    # --- Extensions ---
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')

    # --- 1. vaults ---
    op.create_table(
        'vaults',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_vaults_name', 'vaults', ['name'], unique=True)

    # --- 2. token_usage_logs ---
    op.create_table(
        'token_usage_logs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('timestamp', TIMESTAMP(timezone=True), server_default=sa.func.now(), index=True),
        sa.Column('session_id', sa.String(), nullable=False, index=True),
        sa.Column('models', ARRAY(sa.Text), server_default=sa.text("'{}'::text[]")),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('cost', sa.Float(), nullable=True),
        sa.Column('is_cached', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('context_metadata', JSONB, server_default=sa.text("'{}'::jsonb")),
    )

    # --- 3. notes ---
    op.create_table(
        'notes',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('session_id', sa.Text(), nullable=False, server_default='global'),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('original_text', sa.Text(), nullable=True),
        sa.Column('page_index', JSONB, nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('filestore_path', sa.Text(), nullable=True),
        sa.Column('assets', ARRAY(sa.Text), server_default=sa.text('ARRAY[]::text[]')),
        sa.Column('metadata', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('publish_date', TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_notes_content_hash', 'notes', ['content_hash'])
    op.create_index('ix_notes_session_id', 'notes', ['session_id'])
    op.create_index('ix_notes_publish_date', 'notes', ['publish_date'])

    # --- 4. chunks ---
    op.create_table(
        'chunks',
        sa.Column('id', sa.Uuid(), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('note_id', sa.Uuid(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.Text(), nullable=False, server_default=''),
        sa.Column('status', sa.Text(), nullable=False, server_default='active'),
        sa.Column('embedding', Vector(EMBEDDING_DIM)),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ['note_id'], ['notes.id'], name='chunks_note_fkey', ondelete='CASCADE'
        ),
        sa.CheckConstraint("status IN ('active', 'stale')", name='chunks_status_check'),
        sa.UniqueConstraint('note_id', 'content_hash', name='uq_chunks_note_content_hash'),
    )
    op.create_index('idx_chunks_note_id', 'chunks', ['note_id'])
    op.create_index('idx_chunks_note_index', 'chunks', ['note_id', 'chunk_index'])
    op.execute(
        "CREATE INDEX idx_chunks_text_tsvector ON chunks USING gin (to_tsvector('english', text))"
    )
    op.execute(
        'CREATE INDEX idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)'
    )

    # --- 5. nodes ---
    op.create_table(
        'nodes',
        sa.Column('id', sa.Uuid(), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('note_id', sa.Uuid(), nullable=False),
        sa.Column('block_id', sa.Uuid(), nullable=True),
        sa.Column('node_hash', sa.Text(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('summary', JSONB, nullable=True),
        sa.Column('summary_formatted', sa.Text(), nullable=True),
        sa.Column('level', sa.Integer(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('token_estimate', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.Text(), nullable=False, server_default='active'),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ['note_id'], ['notes.id'], name='nodes_note_fkey', ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['block_id'], ['chunks.id'], name='nodes_block_fkey', ondelete='SET NULL'
        ),
        sa.CheckConstraint("status IN ('active', 'stale')", name='nodes_status_check'),
        sa.UniqueConstraint('note_id', 'node_hash', name='uq_nodes_note_node_hash'),
    )
    op.create_index('idx_nodes_note_id', 'nodes', ['note_id'])
    op.create_index('idx_nodes_block_id', 'nodes', ['block_id'])
    op.execute(
        "CREATE INDEX idx_nodes_text_tsvector ON nodes USING gin (to_tsvector('english', text))"
    )

    # --- 6. entities ---
    op.create_table(
        'entities',
        sa.Column('id', sa.Uuid(), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('canonical_name', sa.Text(), nullable=False),
        sa.Column('phonetic_code', sa.Text(), nullable=True),
        sa.Column('metadata', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('entity_type', sa.Text(), nullable=True),
        sa.Column('first_seen', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('last_seen', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('mention_count', sa.Integer(), server_default='1'),
        sa.Column('retrieval_count', sa.Integer(), server_default='0'),
        sa.Column('last_retrieved_at', TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        'idx_entities_canonical_name_unique', 'entities', ['canonical_name'], unique=True
    )
    op.create_index('ix_entities_phonetic_code', 'entities', ['phonetic_code'])
    op.execute(
        'CREATE INDEX idx_entities_canonical_name_trgm '
        'ON entities USING gin (lower(canonical_name) gin_trgm_ops)'
    )

    # --- 7. entity_aliases ---
    op.create_table(
        'entity_aliases',
        sa.Column('id', sa.Uuid(), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column(
            'canonical_id',
            sa.Uuid(),
            sa.ForeignKey('entities.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('phonetic_code', sa.Text(), nullable=True),
    )
    op.create_index(
        'idx_entity_aliases_canonical_name_unique',
        'entity_aliases',
        ['canonical_id', 'name'],
        unique=True,
    )
    op.create_index('ix_entity_aliases_phonetic_code', 'entity_aliases', ['phonetic_code'])
    op.execute(
        'CREATE INDEX idx_entity_aliases_name_trgm '
        'ON entity_aliases USING gin (lower(name) gin_trgm_ops)'
    )

    # --- 8. memory_units ---
    op.create_table(
        'memory_units',
        sa.Column('id', sa.Uuid(), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('fact_type', sa.Text(), nullable=False, server_default='world'),
        sa.Column('occurred_start', TIMESTAMP(timezone=True), nullable=True),
        sa.Column('occurred_end', TIMESTAMP(timezone=True), nullable=True),
        sa.Column('mentioned_at', TIMESTAMP(timezone=True), nullable=True),
        sa.Column('note_id', sa.Uuid(), nullable=True),
        sa.Column('chunk_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='active'),
        sa.Column('embedding', Vector(EMBEDDING_DIM)),
        sa.Column('context', sa.Text(), nullable=True),
        sa.Column('event_date', TIMESTAMP(timezone=True), nullable=False),
        sa.Column('confidence_alpha', sa.Float(), nullable=True),
        sa.Column('confidence_beta', sa.Float(), nullable=True),
        sa.Column('access_count', sa.Integer(), server_default='0'),
        sa.Column('metadata', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ['note_id'], ['notes.id'], name='memory_units_note_fkey', ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['chunk_id'], ['chunks.id'], name='memory_units_chunk_fkey', ondelete='SET NULL'
        ),
        sa.CheckConstraint("fact_type IN ('world', 'experience', 'opinion')"),
        sa.CheckConstraint("status IN ('active', 'stale')", name='memory_units_status_check'),
        sa.CheckConstraint(
            '(confidence_alpha IS NULL AND confidence_beta IS NULL) '
            'OR ((confidence_alpha IS NOT NULL AND confidence_beta IS NOT NULL) '
            'AND confidence_alpha >= 0.0 AND confidence_beta >= 0.0)'
        ),
        sa.CheckConstraint(
            "(fact_type = 'opinion' AND (confidence_alpha IS NOT NULL AND confidence_beta IS NOT NULL)) OR "
            "(fact_type NOT IN ('opinion') AND confidence_alpha IS NULL AND confidence_beta IS NULL)",
            name='confidence_score_fact_type_check',
        ),
    )
    op.create_index('idx_memory_units_note_id', 'memory_units', ['note_id'])
    op.create_index('idx_memory_units_chunk_id', 'memory_units', ['chunk_id'])
    op.create_index('idx_memory_units_status', 'memory_units', ['status'])
    op.create_index('idx_memory_units_event_date', 'memory_units', [sa.text('event_date DESC')])
    op.create_index('idx_memory_units_access_count', 'memory_units', [sa.text('access_count DESC')])
    op.create_index('idx_memory_units_fact_type', 'memory_units', ['fact_type'])
    op.execute(
        'CREATE INDEX idx_memory_units_embedding ON memory_units '
        'USING hnsw (embedding vector_cosine_ops)'
    )
    op.execute(
        'CREATE INDEX idx_memory_units_embedding_active ON memory_units '
        "USING hnsw (embedding vector_cosine_ops) WHERE status = 'active'"
    )
    op.execute(
        'CREATE INDEX idx_memory_units_embedding_stale ON memory_units '
        "USING hnsw (embedding vector_cosine_ops) WHERE status = 'stale'"
    )

    # --- 9. unit_entities ---
    op.create_table(
        'unit_entities',
        sa.Column(
            'unit_id',
            sa.Uuid(),
            sa.ForeignKey('memory_units.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column(
            'entity_id',
            sa.Uuid(),
            sa.ForeignKey('entities.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
    )
    op.create_index('idx_unit_entities_unit', 'unit_entities', ['unit_id'])
    op.create_index('idx_unit_entities_entity', 'unit_entities', ['entity_id'])

    # --- 10. entity_cooccurrences ---
    op.create_table(
        'entity_cooccurrences',
        sa.Column(
            'entity_id_1',
            sa.Uuid(),
            sa.ForeignKey('entities.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column(
            'entity_id_2',
            sa.Uuid(),
            sa.ForeignKey('entities.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('cooccurrence_count', sa.Integer(), server_default='1'),
        sa.Column('last_cooccurred', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint('entity_id_1 < entity_id_2', name='entity_cooccurrence_order_check'),
    )
    op.create_index('idx_entity_cooccurrences_entity1', 'entity_cooccurrences', ['entity_id_1'])
    op.create_index('idx_entity_cooccurrences_entity2', 'entity_cooccurrences', ['entity_id_2'])
    op.create_index(
        'idx_entity_cooccurrences_count',
        'entity_cooccurrences',
        [sa.text('cooccurrence_count DESC')],
    )

    # --- 11. mental_models ---
    op.create_table(
        'mental_models',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('entity_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('observations', JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column('last_refreshed', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('embedding', Vector(EMBEDDING_DIM), nullable=True),
    )
    op.create_index('ix_mental_models_entity_id', 'mental_models', ['entity_id'])
    op.create_index(
        'idx_mental_models_entity_vault_unique',
        'mental_models',
        ['entity_id', 'vault_id'],
        unique=True,
    )

    # --- 12. reflection_queue ---
    op.create_table(
        'reflection_queue',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column(
            'entity_id',
            sa.Uuid(),
            sa.ForeignKey('entities.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('priority_score', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('accumulated_evidence', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('last_queued_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_retries', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'processing', 'failed', 'dead_letter')"),
    )
    op.create_index(
        'idx_reflection_queue_priority',
        'reflection_queue',
        [sa.text('priority_score DESC')],
    )
    op.create_index('idx_reflection_queue_status', 'reflection_queue', ['status'])
    op.create_index(
        'idx_reflection_queue_entity_vault',
        'reflection_queue',
        ['entity_id', 'vault_id'],
    )

    # --- 13. batch_jobs ---
    op.create_table(
        'batch_jobs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
        sa.Column('progress', sa.Text(), nullable=True),
        sa.Column('result', JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column('notes_count', sa.Integer(), server_default='0'),
        sa.Column('processed_count', sa.Integer(), server_default='0'),
        sa.Column('skipped_count', sa.Integer(), server_default='0'),
        sa.Column('failed_count', sa.Integer(), server_default='0'),
        sa.Column('note_ids', JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column('error_info', JSONB, nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('started_at', TIMESTAMP(timezone=True), nullable=True),
        sa.Column('completed_at', TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index('idx_batch_jobs_status', 'batch_jobs', ['status'])

    # --- 14. evidence_log ---
    op.create_table(
        'evidence_log',
        sa.Column('id', sa.Uuid(), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column(
            'unit_id',
            sa.Uuid(),
            sa.ForeignKey('memory_units.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('evidence_type', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('alpha_before', sa.Float(), nullable=False),
        sa.Column('beta_before', sa.Float(), nullable=False),
        sa.Column('alpha_after', sa.Float(), nullable=False),
        sa.Column('beta_after', sa.Float(), nullable=False),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_evidence_log_unit_id', 'evidence_log', ['unit_id'])
    op.create_index('idx_evidence_log_created_at', 'evidence_log', ['created_at'])

    # --- 15. memory_links ---
    op.create_table(
        'memory_links',
        sa.Column(
            'from_unit_id',
            sa.Uuid(),
            sa.ForeignKey('memory_units.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column(
            'to_unit_id',
            sa.Uuid(),
            sa.ForeignKey('memory_units.id', ondelete='CASCADE'),
            primary_key=True,
        ),
        sa.Column('vault_id', sa.Uuid(), nullable=False),
        sa.Column('link_type', sa.Text(), primary_key=True),
        sa.Column(
            'entity_id',
            sa.Uuid(),
            sa.ForeignKey('entities.id', ondelete='CASCADE'),
            nullable=True,
        ),
        sa.Column('weight', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "link_type IN ('temporal', 'semantic', 'entity', 'causes', 'caused_by', 'enables', 'prevents')",
            name='memory_links_link_type_check',
        ),
        sa.CheckConstraint('weight >= 0.0 AND weight <= 1.0', name='memory_links_weight_check'),
    )
    op.create_index('idx_memory_links_from', 'memory_links', ['from_unit_id'])
    op.create_index('idx_memory_links_to', 'memory_links', ['to_unit_id'])
    op.create_index('idx_memory_links_type', 'memory_links', ['link_type'])
    op.execute(
        'CREATE INDEX idx_memory_links_entity ON memory_links (entity_id) '
        'WHERE entity_id IS NOT NULL'
    )
    op.execute(
        'CREATE INDEX idx_memory_links_from_weight ON memory_links (from_unit_id, weight DESC) '
        'WHERE weight >= 0.1'
    )

    # --- 16. audit_logs ---
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('timestamp', TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column('actor', sa.String(255), nullable=True),
        sa.Column('action', sa.String(100), nullable=False),
        sa.Column('resource_type', sa.String(100), nullable=True),
        sa.Column('resource_id', sa.String(255), nullable=True),
        sa.Column('session_id', sa.String(255), nullable=True),
        sa.Column('details', JSONB, nullable=True),
    )
    op.create_index('idx_audit_logs_timestamp', 'audit_logs', ['timestamp'])
    op.create_index('idx_audit_logs_actor', 'audit_logs', ['actor'])
    op.create_index('idx_audit_logs_action', 'audit_logs', ['action'])
    op.create_index('idx_audit_logs_resource', 'audit_logs', ['resource_type', 'resource_id'])

    # --- 17. webhook_registrations ---
    op.create_table(
        'webhook_registrations',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('url', sa.String(2048), nullable=False),
        sa.Column('secret', sa.String(255), nullable=False),
        sa.Column('events', ARRAY(sa.Text), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_webhook_registrations_active', 'webhook_registrations', ['active'])

    # --- 18. webhook_deliveries ---
    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column(
            'webhook_id',
            sa.Uuid(),
            sa.ForeignKey('webhook_registrations.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('event', sa.String(100), nullable=False),
        sa.Column('payload', JSONB, nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('idx_webhook_deliveries_webhook_id', 'webhook_deliveries', ['webhook_id'])
    op.create_index('idx_webhook_deliveries_status', 'webhook_deliveries', ['status'])


def downgrade() -> None:
    # Drop tables in reverse FK dependency order
    op.drop_table('webhook_deliveries')
    op.drop_table('webhook_registrations')
    op.drop_table('audit_logs')
    op.drop_table('memory_links')
    op.drop_table('evidence_log')
    op.drop_table('batch_jobs')
    op.drop_table('reflection_queue')
    op.drop_table('mental_models')
    op.drop_table('entity_cooccurrences')
    op.drop_table('unit_entities')
    op.drop_table('memory_units')
    op.drop_table('entity_aliases')
    op.drop_table('entities')
    op.drop_table('nodes')
    op.drop_table('chunks')
    op.drop_table('notes')
    op.drop_table('token_usage_logs')
    op.drop_table('vaults')

    # Drop extensions
    op.execute('DROP EXTENSION IF EXISTS pg_trgm')
    op.execute('DROP EXTENSION IF EXISTS vector')
