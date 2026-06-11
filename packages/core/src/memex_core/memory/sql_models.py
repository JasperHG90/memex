from datetime import datetime, timezone
from enum import Enum, StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, field_validator
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    Computed,
    ForeignKey,
    Integer,
    String,
    Text,
    Float,
    func,
    text as sql_text,
    Index,
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, ARRAY, TSVECTOR
from sqlalchemy import event
from sqlalchemy.types import Uuid as SA_UUID
from sqlmodel import SQLModel, Field, Relationship

from memex_core.context import get_session_id
from memex_core.memory.mixins import vault_id_field, created_at_field, updated_at_field

from memex_common.schemas import MemoryUnitBase, FactTypes
from memex_common.vault_policy import VaultKind

EMBEDDING_DIMENSION = 384


class ContentStatus(str, Enum):
    """Status of content units (chunks, memory units)."""

    ACTIVE = 'active'
    STALE = 'stale'


class MWMode(StrEnum):
    """Memory Worth counter mode per vault."""

    STATIONARY = 'stationary'
    EMA = 'ema'


class Vault(SQLModel, table=True):  # type: ignore
    """
    What it is: A logical grouping of memories and knowledge.
    Function: Allows multi-tenancy or project-based isolation.
    """

    __tablename__ = 'vaults'

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description='Unique identifier for the vault.',
    )
    name: str = Field(index=True, unique=True, description='The name of the vault.')
    description: str | None = Field(default=None, description='Optional description of the vault.')
    mw_mode: MWMode = Field(
        default=MWMode.STATIONARY,
        sa_column=Column(Text, nullable=False, server_default='stationary'),
    )
    kind: VaultKind = Field(
        default=VaultKind.CONTENT,
        sa_column=Column(Text, nullable=False, server_default='content'),
    )
    policy: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the vault was created.',
    )

    __table_args__ = (
        CheckConstraint(
            "mw_mode IN ('stationary', 'ema')",
            name='vaults_mw_mode_check',
        ),
        CheckConstraint(
            "kind IN ('content', 'system')",
            name='vaults_kind_check',
        ),
    )


class Trend(str, Enum):
    """Reflects the trajectory of an observation."""

    NEW = 'new'
    STABLE = 'stable'
    STRENGTHENING = 'strengthening'
    WEAKENING = 'weakening'
    STALE = 'stale'


class EvidenceItem(BaseModel):
    """Supporting evidence for an observation."""

    memory_id: UUID = Field(description='The UUID of the source memory unit.')
    quote: str | None = Field(default=None, description='The exact quote from the source memory.')
    relevance: float = Field(
        default=1.0, description='Relevance score of this evidence (0.0 to 1.0).'
    )
    explanation: str | None = Field(
        default=None, description='Explanation of why this evidence supports the observation.'
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), description='Timestamp of the evidence.'
    )


class Observation(BaseModel):
    """A synthesized insight about an entity."""

    id: UUID = Field(default_factory=uuid4, description='Unique identifier for the observation.')
    title: str = Field(description='Short title or headline for the observation.')
    content: str = Field(description='Detailed content of the observation.')
    trend: Trend = Field(
        default=Trend.NEW,
        description='Current trend of this observation (e.g. new, stable, strengthening).',
    )
    evidence: list[EvidenceItem] = Field(
        default=[], description='List of supporting evidence items.'
    )


class MentalModel(SQLModel, table=True):  # type: ignore
    """
    What it is: A synthesized 'mental model' of an entity.
    Function: Aggregates observations and trends to provide a higher-level understanding.
    """

    __tablename__ = 'mental_models'

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description='Unique identifier for the mental model.',
    )
    vault_id: UUID = vault_id_field()
    entity_id: UUID = Field(index=True, description='The UUID of the entity this model describes.')
    name: str = Field(description='The canonical name of the entity.')

    # Use list[dict] for JSONB to avoid serialization issues with Pydantic models
    observations: list[dict[str, Any]] = Field(
        default=[],
        sa_column=Column(JSONB, server_default=sql_text("'[]'::jsonb")),
        description='Synthesized observations about this entity, stored as a list of JSON-serialized Observation objects.',
    )

    entity_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, server_default=sql_text("'{}'::jsonb")),
        description='Structured metadata derived from observations (description, category, status).',
    )

    last_refreshed: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Last time this model was updated by the reflection engine.',
    )
    version: int = Field(
        default=1, description='Version number of the mental model, incremented on each update.'
    )
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(EMBEDDING_DIMENSION)),
        description='Semantic embedding of the mental model (centroid of observation embeddings).',
    )

    success_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Memory Worth success co-occurrence counter (vault-scoped).',
    )

    failure_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Memory Worth failure co-occurrence counter (vault-scoped).',
    )

    unused_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description=(
            'Engagement counter: bumped when a unit was retrieved but the '
            'caller marked it as not_used. Does NOT enter the Beta-Bernoulli '
            'posterior (engagement-only signal).'
        ),
    )

    archived_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description=(
            'Soft-delete timestamp set by the archive_mental_model proposal action; '
            'rows with a non-NULL archived_at are hidden from retrieval, survey, and '
            'reflection enumeration. Reversed by clearing the column.'
        ),
    )

    __table_args__ = (
        # Enforce uniqueness for Entity + Vault (Global or Specific)
        Index(
            'idx_mental_models_entity_vault_unique',
            'entity_id',
            'vault_id',
            unique=True,
        ),
        # JSONB containment GIN — supports vault-scoped scans for observations
        # citing a deprioritized MU (deprio → refresh-task enqueue path).
        # Created by migration 044. Declared here so alembic autogenerate does
        # NOT emit a spurious drop_index for it on future revisions.
        Index(
            'idx_mental_models_observations_gin',
            'observations',
            postgresql_using='gin',
            postgresql_ops={'observations': 'jsonb_path_ops'},
        ),
        # Partial index on archived rows — query path filters WHERE archived_at IS NULL,
        # which the planner short-circuits using this index when populated.
        Index(
            'idx_mental_models_archived_at',
            'archived_at',
            postgresql_where=sql_text('archived_at IS NOT NULL'),
        ),
    )


class Note(SQLModel, table=True):  # type: ignore
    """
    What it is: The raw container for information.
    Function: Represents a file, an email, a chat log, or a web page that was ingested into the system.
    Key Features:
        - content_hash: Used to prevent duplicate processing of the same file.
        - doc_metadata: A JSONB field to store arbitrary source info (author, URL, file path) without changing the schema.
        - Relationship: One Note splits into many MemoryUnits.
    """

    __tablename__ = 'notes'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True),
        description='Unique identifier for the note.',
    )

    vault_id: UUID = vault_id_field()

    session_id: str = Field(
        default_factory=get_session_id,
        sa_column=Column(Text, nullable=False, server_default='global', index=True),
        description='The session identifier during which this note was ingested.',
    )

    title: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Resolved human-readable title for the note.',
    )

    description: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Short description synthesized from block summaries or content truncation.',
    )

    original_text: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description='The full, raw text content of the note.',
    )

    page_index: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description='Thin tree structure (TOC with node IDs, titles, levels, summaries). '
        'Only populated when page_index extraction strategy is used.',
    )

    content_hash: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description='MD5 hash of the original text, used for deduplication.',
    )

    filestore_path: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description='Path to the original file in the filestore, if applicable.',
    )

    assets: list[str] = Field(
        default=[],
        sa_column=Column(ARRAY(Text), server_default=sql_text('ARRAY[]::text[]')),
        description='List of associated asset file paths (e.g. images, PDFs).',
    )

    doc_metadata: dict[str, Any] = Field(
        default={},
        sa_column=Column('metadata', JSONB, server_default=sql_text("'{}'::jsonb")),
        description='Arbitrary metadata about the source (URL, author, timestamp).',
    )

    publish_date: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True, index=True),
        description='The publication or event date of the document content.',
    )

    status: str = Field(
        default='active',
        sa_column=Column(
            Text,
            nullable=False,
            server_default='active',
            index=True,
        ),
        description='Note lifecycle status: active or superseded. '
        'Append intent is recorded via the dedicated append endpoint; '
        'archive intent is recorded in archived_at + FSFM (units deprioritized).',
    )

    superseded_by: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='ID of the note that supersedes this one.',
    )

    appended_to: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='ID of the note this one was appended to.',
    )

    archived_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True, index=True),
        description='Human-intent archive timestamp. Non-NULL means the note '
        'was archived; the cascade lives in FSFM via '
        'MemoryUnit.is_deprioritized=true rather than in the status enum.',
    )

    summary_version_incorporated: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description='VaultSummary.version when this note was last incorporated into the summary. '
        'NULL or < current version means pending.',
    )

    role: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='classification of note provenance within the procedural plane. '
        'NULL for ordinary declarative-plane notes. One of '
        "'case' (raw or derived experience record — parent of a procedure), "
        "'procedure' (a how-to recipe synthesised from one or more cases), "
        "'strategy' (an opinionated play-book that picks a procedure for a context). "
        'Backed by a CHECK constraint and a partial index; see migration 062.',
    )

    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()

    # Relationships
    memory_units: list['MemoryUnit'] = Relationship(
        back_populates='note', sa_relationship_kwargs={'cascade': 'all, delete-orphan'}
    )
    chunks: list['Chunk'] = Relationship(
        back_populates='note', sa_relationship_kwargs={'cascade': 'all, delete-orphan'}
    )

    __table_args__ = (
        Index('idx_notes_content_hash', 'content_hash'),
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name='ck_notes_status',
        ),
        CheckConstraint(
            "role IS NULL OR role IN ('case', 'procedure', 'strategy')",
            name='ck_notes_role',
        ),
        Index(
            'idx_notes_title_trgm',
            sql_text('lower(title) gin_trgm_ops'),
            postgresql_using='gin',
        ),
        Index('idx_notes_summary_version', 'vault_id', 'summary_version_incorporated'),
        Index(
            'idx_notes_role',
            'vault_id',
            'role',
            postgresql_where=sql_text('role IS NOT NULL'),
        ),
    )


class Chunk(SQLModel, table=True):  # type: ignore
    """
    What it is: A content-addressed paragraph block from a Document.
    Function: Preserves the original text structure for traceability and enables
    incremental diffing via content hashing.
    Key Features:
        - content_hash: SHA-256 hash for identity-based diffing across document versions.
        - status: Active or stale (marked stale during incremental updates, never deleted).
        - chunk_index: Maintains the order of the text within the document.
        - text: The raw text of the chunk.
    """

    __tablename__ = 'chunks'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the chunk.',
    )
    vault_id: UUID = vault_id_field()
    note_id: UUID = Field(sa_column=Column(SA_UUID()), description='Identifier of the source note.')
    text: str = Field(
        sa_column=Column(Text, nullable=False),
        description='The raw text content of the chunk.',
    )
    content_hash: str = Field(
        sa_column=Column(Text, nullable=False, server_default=''),
        description='SHA-256 hash of whitespace-normalized text for incremental diffing.',
    )
    status: ContentStatus = Field(
        sa_column=Column(Text, nullable=False, server_default='active'),
        description='Content status: active or stale.',
    )
    embedding: list[float] = Field(
        sa_column=Column(Vector(EMBEDDING_DIMENSION)),
        description='Vector embedding representation of the raw chunk text.',
    )
    chunk_index: int = Field(
        sa_column=Column(Integer, nullable=False),
        description='The sequential index of this chunk within the document.',
    )
    summary: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description='Block-level summary blob: {"topic": ..., "key_points": [...]}',
    )
    summary_formatted: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Pre-formatted block summary: "topic — point1 | point2 | ..."',
    )
    created_at: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the chunk was created.',
    )

    # Relationships
    note: Note = Relationship(back_populates='chunks')
    memory_units: list['MemoryUnit'] = Relationship(back_populates='chunk')
    nodes: list['Node'] = Relationship(
        back_populates='chunk', sa_relationship_kwargs={'cascade': 'all, delete-orphan'}
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ['note_id'],
            ['notes.id'],
            name='chunks_note_fkey',
            ondelete='CASCADE',
        ),
        CheckConstraint("status IN ('active', 'stale')", name='chunks_status_check'),
        UniqueConstraint('note_id', 'content_hash', name='uq_chunks_note_content_hash'),
        Index('idx_chunks_note_id', 'note_id'),
        Index('idx_chunks_note_index', 'note_id', 'chunk_index'),
        Index(
            'idx_chunks_text_tsvector',
            sql_text("to_tsvector('english', text)"),
            postgresql_using='gin',
        ),
        Index(
            'idx_chunks_embedding',
            'embedding',
            postgresql_using='hnsw',
            postgresql_ops={'embedding': 'vector_cosine_ops'},
        ),
    )


class Node(SQLModel, table=True):  # type: ignore
    """
    What it is: A section-level text unit from a Document, produced by PageIndex.
    Function: Nodes are the single source of truth for text content. Each node
    represents a section (or subsection) in the document hierarchy. Blocks (chunks)
    aggregate one or more nodes and hold the embedding.
    """

    __tablename__ = 'nodes'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the node.',
    )
    vault_id: UUID = vault_id_field()
    note_id: UUID = Field(
        sa_column=Column(SA_UUID(), nullable=False),
        description='Identifier of the source note.',
    )
    block_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID()),
        description='Identifier of the block (chunk) this node belongs to. Nullable until block assignment.',
    )
    node_hash: str = Field(
        sa_column=Column(Text, nullable=False),
        description='MD5 hash of node content for incremental diffing.',
    )
    title: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Section title.',
    )
    text: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Full text content of the node.',
    )
    summary: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description='SectionSummary blob: {"who": ..., "what": ..., "how": ..., "when": ..., "where": ...}',
    )
    summary_formatted: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description='Pre-formatted summary: "who | what | how | when | where"',
    )
    level: int = Field(
        sa_column=Column(Integer, nullable=False),
        description='Hierarchy level (1=H1, 2=H2, etc.).',
    )
    seq: int = Field(
        sa_column=Column(Integer, nullable=False),
        description='Sequential order within the document.',
    )
    token_estimate: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description='Token count of the text.',
    )
    status: ContentStatus = Field(
        sa_column=Column(Text, nullable=False, server_default='active'),
        description='Content status: active or stale.',
    )
    assets: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb")),
        description='Per-section embedded image refs: [{"path", "alt_text", "filename"}].',
    )
    created_at: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the node was created.',
    )

    # Relationships
    chunk: Chunk | None = Relationship(back_populates='nodes')

    __table_args__ = (
        ForeignKeyConstraint(
            ['note_id'],
            ['notes.id'],
            name='nodes_note_fkey',
            ondelete='CASCADE',
        ),
        ForeignKeyConstraint(
            ['block_id'],
            ['chunks.id'],
            name='nodes_block_fkey',
            ondelete='SET NULL',
        ),
        CheckConstraint("status IN ('active', 'stale')", name='nodes_status_check'),
        UniqueConstraint('note_id', 'node_hash', name='uq_nodes_note_node_hash'),
        Index('idx_nodes_note_id', 'note_id'),
        Index('idx_nodes_block_id', 'block_id'),
        Index(
            'idx_nodes_text_tsvector',
            sql_text("to_tsvector('english', text)"),
            postgresql_using='gin',
        ),
        # B.2 — partial covering index for the document_search nodes-keyword
        # CTE (filters `tsvector @@ ts_query AND block_id IS NOT NULL AND
        # status='active' AND vault_id IN (...)`). The tsvector GIN covers
        # the fulltext predicate; this index covers the remaining
        # vault-scope + liveness checks so they don't bitmap-recheck.
        # Mirrors alembic migration 054_nodes_vault_active.
        Index(
            'idx_nodes_vault_active',
            'vault_id',
            postgresql_where=sql_text("status = 'active' AND block_id IS NOT NULL"),
        ),
    )


class MemoryUnit(SQLModel, MemoryUnitBase, table=True):  # type: ignore
    """
    SQLModel implementation of a Memory Unit.
    Matches the Hindsight 'Facts' concept.
    """

    __tablename__ = 'memory_units'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the memory unit.',
    )

    # Inherited Fields Overrides for SQLModel Mapping

    vault_id: UUID = vault_id_field()

    text: str = Field(
        sa_column=Column(Text, nullable=False),
        description='The textual content of the memory unit.',
    )

    fact_type: FactTypes = Field(
        sa_column=Column(Text, nullable=False, server_default='world'),
        description='The type/category of the memory unit: world, event, or observation.',
    )

    occurred_start: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),
        description='The start datetime of when the fact/event occurred, if applicable.',
    )

    occurred_end: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),
        description='The end datetime of when the fact/event occurred, if applicable.',
    )

    mentioned_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),
        description='The datetime when the memory unit was mentioned, if applicable.',
    )

    note_id: UUID | None = Field(
        default=None, sa_column=Column(SA_UUID()), description='Identifier of the source note.'
    )

    chunk_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID()),
        description='Identifier of the source chunk. Nullable for backward compatibility.',
    )

    status: ContentStatus = Field(
        sa_column=Column(Text, nullable=False, server_default='active'),
        description='Content status: active or stale.',
    )

    embedding: list[float] = Field(
        sa_column=Column(Vector(EMBEDDING_DIMENSION)),
        description='Vector embedding representation of the memory unit text.',
    )

    context: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description='Additional context associated with the memory unit.',
    )

    event_date: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), nullable=False),
        description='The date when the memory unit was created or is relevant.',
    )

    success_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Number of successful outcome co-occurrences for Memory Worth scoring.',
    )

    failure_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Number of failure outcome co-occurrences for Memory Worth scoring.',
    )

    unused_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description=(
            'Engagement counter: bumped when a unit was retrieved but the '
            'caller marked it as not_used. Does NOT enter the Beta-Bernoulli '
            'posterior (engagement-only signal).'
        ),
    )

    is_deprioritized: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default='false'),
        description='Whether this unit has been deprioritized (non-destructive retrieval downweight).',
    )

    intent_class: str = Field(
        default='durable',
        sa_column=Column(Text, nullable=False, server_default='durable'),
        description='Lifecycle class set at write time: permanent | durable | ephemeral.',
    )

    risk_class: str = Field(
        default='none',
        sa_column=Column(Text, nullable=False, server_default='none'),
        description='Content sensitivity set at write time: none | sensitive | private | safety.',
    )

    claim_type: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description=(
            'Explicit corrective-claim signal: resolution | contradiction | NULL. '
            'NULL means the unit was not extracted as an explicit claim.'
        ),
    )

    confidence: float = Field(
        default=1.0,
        sa_column=Column(Float, nullable=False, server_default='1.0'),
        description='Confidence score (0.0-1.0). Decreased when contradicted by newer information.',
    )

    confidence_evidence_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description=(
            'Negative-evidence event count — how many times the contradiction '
            'engine has weakened or contradicted this unit. Pairs with the closed-form '
            'Beta(1, 1) posterior at memex_core.memory.confidence.mean_and_variance to '
            'derive variance without storing it. Cold-start (count=0) → variance = 1/12.'
        ),
    )

    importance: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
        description=(
            'Importance signal derived from intent_class at write time '
            '(permanent=1.0, durable=0.7, ephemeral=0.3). NULL for '
            'unclassified units; the decay boost treats NULL as no signal '
            '-> neutral 1.0.'
        ),
    )

    stability: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
        description=(
            'Per-intent-class stability in days (durable=180, ephemeral=14, '
            'permanent=NULL meaning infinity). The decay boost treats '
            'NULL as the stability -> infinity limit (decay term = 1.0).'
        ),
    )

    last_outcome_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description=(
            'Wall-clock timestamp of the most recent record_outcome call. '
            'NULL on units that have never had an outcome recorded; the '
            'decay boost treats NULL as no temporal anchor -> neutral 1.0.'
        ),
    )

    unit_metadata: dict[str, Any] = Field(
        default={},
        sa_column=Column('metadata', JSONB, server_default=sql_text("'{}'::jsonb")),
        description='Additional metadata associated with the memory unit.',
    )

    search_tsvector: Any = Field(
        default=None,
        sa_column=Column(
            TSVECTOR,
            Computed(
                "to_tsvector('english', "
                "coalesce(text, '') || ' ' || "
                "coalesce(metadata->>'tags', '') || ' ' || "
                "coalesce(metadata->>'enriched_tags', '') || ' ' || "
                "coalesce(metadata->>'enriched_keywords', ''))",
                persisted=True,
            ),
        ),
    )

    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()

    note: Note | None = Relationship(back_populates='memory_units')
    chunk: Chunk | None = Relationship(back_populates='memory_units')
    unit_entities: list['UnitEntity'] = Relationship(
        back_populates='memory_unit', sa_relationship_kwargs={'cascade': 'all, delete-orphan'}
    )
    outgoing_links: list['MemoryLink'] = Relationship(
        back_populates='from_unit',
        sa_relationship_kwargs={
            'cascade': 'all, delete-orphan',
            'foreign_keys': 'MemoryLink.from_unit_id',
        },
    )
    incoming_links: list['MemoryLink'] = Relationship(
        back_populates='to_unit',
        sa_relationship_kwargs={
            'cascade': 'all, delete-orphan',
            'foreign_keys': 'MemoryLink.to_unit_id',
        },
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ['note_id'],
            ['notes.id'],
            name='memory_units_note_fkey',
            ondelete='CASCADE',
        ),
        ForeignKeyConstraint(
            ['chunk_id'],
            ['chunks.id'],
            name='memory_units_chunk_fkey',
            ondelete='SET NULL',
        ),
        CheckConstraint("fact_type IN ('world', 'event', 'observation')"),
        CheckConstraint("status IN ('active', 'stale')", name='memory_units_status_check'),
        CheckConstraint(
            "intent_class IN ('permanent', 'durable', 'ephemeral')",
            name='ck_memory_units_intent_class',
        ),
        CheckConstraint(
            "risk_class IN ('none', 'sensitive', 'private', 'safety')",
            name='ck_memory_units_risk_class',
        ),
        CheckConstraint(
            "claim_type IS NULL OR claim_type IN ('resolution', 'contradiction')",
            name='ck_memory_units_claim_type',
        ),
        CheckConstraint(
            'confidence >= 0.0 AND confidence <= 1.0',
            name='memory_units_confidence_check',
        ),
        CheckConstraint(
            'confidence_evidence_count >= 0',
            name='memory_units_confidence_evidence_count_check',
        ),
        Index('idx_memory_units_note_id', 'note_id'),
        Index('idx_memory_units_chunk_id', 'chunk_id'),
        Index('idx_memory_units_status', 'status'),
        Index('idx_memory_units_event_date', 'event_date', postgresql_ops={'event_date': 'DESC'}),
        Index(
            'idx_memory_units_is_deprioritized',
            'is_deprioritized',
            postgresql_where=sql_text('is_deprioritized = true'),
        ),
        Index('idx_memory_units_fact_type', 'fact_type'),
        Index('idx_memory_units_confidence', 'confidence'),
        Index(
            'idx_memory_units_embedding',
            'embedding',
            postgresql_using='hnsw',
            postgresql_ops={'embedding': 'vector_cosine_ops'},
        ),
        Index(
            'idx_memory_units_embedding_active',
            'embedding',
            postgresql_using='hnsw',
            postgresql_ops={'embedding': 'vector_cosine_ops'},
            postgresql_where=sql_text("status = 'active'"),
        ),
        Index(
            'idx_memory_units_embedding_stale',
            'embedding',
            postgresql_using='hnsw',
            postgresql_ops={'embedding': 'vector_cosine_ops'},
            postgresql_where=sql_text("status = 'stale'"),
        ),
        Index(
            'idx_memory_units_search_tsvector',
            'search_tsvector',
            postgresql_using='gin',
        ),
        Index(
            'ix_memory_units_context',
            'context',
            postgresql_where=sql_text('context IS NOT NULL'),
        ),
    )

    @property
    def formatted_fact_text(self) -> str:
        """
        Returns the standard string representation for LLM contexts.
        Format: "[YYYY-MM-DD] The memory text." or "[STALE] [YYYY-MM-DD] The memory text."
        Includes nested citations if available in metadata.
        """
        date_str = (
            self.occurred_start.strftime('%Y-%m-%d') if self.occurred_start else 'Unknown Date'
        )
        status_prefix = '[STALE] ' if self.status == ContentStatus.STALE else ''
        base_text = f'{status_prefix}[{date_str}] {self.text}'

        # Append citations if present (from RetrievalEngine deduplication)
        citations = self.unit_metadata.get('citations', [])
        if citations:
            citation_lines = []
            for c in citations:
                # Handle both dicts (runtime) and objects (if changed later)
                c_text = c.get('text') if isinstance(c, dict) else getattr(c, 'text', str(c))
                c_date = c.get('date', '') if isinstance(c, dict) else getattr(c, 'event_date', '')

                # Format: "  - [YYYY-MM-DD] Evidence text"
                citation_lines.append(f'  - [{c_date[:10]}] {c_text}')

            if citation_lines:
                base_text += '\n' + '\n'.join(citation_lines)

        return base_text


class Entity(SQLModel, table=True):  # type: ignore
    """
    What it is: A specific person, place, organization, or concept found within the text.
    Function: Forms the nodes of a Knowledge Graph.
    Key Features:
        - canonical_name: The standardized name (e.g., normalizing "J. Doe" and "John Doe" to "John Doe").
        - mention_count: Tracks importance. The more an entity is mentioned across different documents, the higher this number.
        - *_seen dates: Tracks the timeline of an entity's presence in the corpus.
    """

    __tablename__ = 'entities'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the entity.',
    )
    canonical_name: str = Field(
        sa_column=Column(Text, nullable=False),
        description='The canonical, standardized name of the entity.',
    )

    phonetic_code: str | None = Field(
        default=None,
        sa_column=Column(Text, index=True),
        description='Double Metaphone phonetic code for the canonical name.',
    )

    entity_type: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description='NER-derived entity type (Person, Organization, Location, Concept).',
    )

    first_seen: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the entity was first encountered in the corpus.',
    )
    last_seen: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the entity was most recently encountered.',
    )
    mention_count: int = Field(
        default=1,
        sa_column=Column(Integer, server_default='1'),
        description='Cumulative number of times this entity has been mentioned across all documents.',
    )
    retrieval_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Cumulative number of times this entity has been retrieved in search results.',
    )
    last_retrieved_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),
        description='Timestamp when the entity was most recently returned in a retrieval result.',
    )
    last_merge_scan_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description=(
            'Timestamp of the most recent inclusion in a cross-batch entity-merge scan. '
            'NULL = never scanned (eligible on the first pass).'
        ),
    )

    unit_entities: list['UnitEntity'] = Relationship(
        back_populates='entity', sa_relationship_kwargs={'cascade': 'all, delete-orphan'}
    )
    aliases: list['EntityAlias'] = Relationship(
        back_populates='entity', sa_relationship_kwargs={'cascade': 'all, delete-orphan'}
    )
    memory_links: list['MemoryLink'] = Relationship(
        back_populates='entity', sa_relationship_kwargs={'cascade': 'all, delete-orphan'}
    )
    cooccurrences_1: list['EntityCooccurrence'] = Relationship(
        back_populates='entity_1',
        sa_relationship_kwargs={
            'cascade': 'all, delete-orphan',
            'foreign_keys': 'EntityCooccurrence.entity_id_1',
        },
    )
    cooccurrences_2: list['EntityCooccurrence'] = Relationship(
        back_populates='entity_2',
        sa_relationship_kwargs={
            'cascade': 'all, delete-orphan',
            'foreign_keys': 'EntityCooccurrence.entity_id_2',
        },
    )

    __table_args__ = (
        Index('idx_entities_canonical_name_unique', 'canonical_name', unique=True),
        Index(
            'idx_entities_canonical_name_trgm',
            sql_text('lower(canonical_name) gin_trgm_ops'),
            postgresql_using='gin',
        ),
        Index(
            'idx_entities_last_merge_scan_at',
            'last_merge_scan_at',
            postgresql_where=sql_text('last_merge_scan_at IS NOT NULL'),
        ),
    )


class EntityAlias(SQLModel, table=True):  # type: ignore
    """
    What it is: An alternate name for an entity.
    Function: Allows lookup by nickname, abbreviation, or former name.
    """

    __tablename__ = 'entity_aliases'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the alias.',
    )
    canonical_id: UUID = Field(
        sa_column=Column(SA_UUID(), ForeignKey('entities.id', ondelete='CASCADE'), nullable=False),
        description='UUID of the canonical entity.',
    )
    name: str = Field(
        sa_column=Column(Text, nullable=False),
        description='The alias name.',
    )
    phonetic_code: str | None = Field(
        default=None,
        sa_column=Column(Text, index=True),
        description='Double Metaphone phonetic code for the alias.',
    )

    # Relationships
    entity: Entity = Relationship(back_populates='aliases')

    __table_args__ = (
        Index('idx_entity_aliases_canonical_name_unique', 'canonical_id', 'name', unique=True),
        Index(
            'idx_entity_aliases_name_trgm',
            sql_text('lower(name) gin_trgm_ops'),
            postgresql_using='gin',
        ),
    )


class UnitEntity(SQLModel, table=True):  # type: ignore
    """
    What it is: A "Join Table" (Many-to-Many connection).
    Function: Links a MemoryUnit to the Entitys mentioned inside it.
    Example: If a MemoryUnit says "Elon Musk bought Twitter," this table creates two rows linking that Unit ID to the Entity ID for "Elon Musk" and the Entity ID for "Twitter".
    Technical Note: It uses cascading deletes. If the MemoryUnit is deleted, these links vanish automatically.
    """

    __tablename__ = 'unit_entities'

    unit_id: UUID = Field(
        primary_key=True,
        sa_column_args=[ForeignKey('memory_units.id', ondelete='CASCADE')],
        description='UUID of the memory unit.',
    )

    entity_id: UUID = Field(
        primary_key=True,
        sa_column_args=[ForeignKey('entities.id', ondelete='CASCADE')],
        description='UUID of the entity mentioned in the unit.',
    )

    vault_id: UUID = vault_id_field()

    success_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Memory Worth success co-occurrence counter (vault-scoped).',
    )

    failure_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Memory Worth failure co-occurrence counter (vault-scoped).',
    )

    unused_co_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description=(
            'Engagement counter: bumped when a unit was retrieved but the '
            'caller marked it as not_used. Does NOT enter the Beta-Bernoulli '
            'posterior (engagement-only signal).'
        ),
    )

    # Relationships

    memory_unit: 'MemoryUnit' = Relationship(back_populates='unit_entities')

    entity: 'Entity' = Relationship(back_populates='unit_entities')

    __table_args__ = (
        Index('idx_unit_entities_unit', 'unit_id'),
        Index('idx_unit_entities_entity', 'entity_id'),
    )


class EntityCooccurrence(SQLModel, table=True):  # type: ignore
    """


    What it is: A cache of how often two entities appear together.


    Function: This builds the "Social Network" of your data.


    Constraint: entity_id_1 < entity_id_2 ensures edges are undirected and unique. You won't store "Apple + Steve Jobs" and "Steve Jobs + Apple" as two different rows; they are forced into one canonical pair.


    Use Case: Allows you to query "Who is most closely related to Entity X?" without doing expensive joins across the entire memory table.


    """

    __tablename__ = 'entity_cooccurrences'

    entity_id_1: UUID = Field(
        primary_key=True,
        sa_column_args=[ForeignKey('entities.id', ondelete='CASCADE')],
        description='UUID of the first entity (lexicographically smaller).',
    )

    entity_id_2: UUID = Field(
        primary_key=True,
        sa_column_args=[ForeignKey('entities.id', ondelete='CASCADE')],
        description='UUID of the second entity (lexicographically larger).',
    )

    vault_id: UUID = vault_id_field(primary_key=True)

    cooccurrence_count: int = Field(
        default=1,
        sa_column=Column(Integer, server_default='1'),
        description='Number of times these two entities have appeared together.',
    )

    last_cooccurred: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp of the most recent cooccurrence.',
    )

    valid_from: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='Start of this relation validity interval. NULL = open-start.',
    )

    valid_to: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='End of this relation validity interval. NULL = still valid.',
    )

    # Relationships

    entity_1: 'Entity' = Relationship(
        back_populates='cooccurrences_1',
        sa_relationship_kwargs={'foreign_keys': '[EntityCooccurrence.entity_id_1]'},
    )

    entity_2: 'Entity' = Relationship(
        back_populates='cooccurrences_2',
        sa_relationship_kwargs={'foreign_keys': '[EntityCooccurrence.entity_id_2]'},
    )

    __table_args__ = (
        CheckConstraint('entity_id_1 < entity_id_2', name='entity_cooccurrence_order_check'),
        Index('idx_entity_cooccurrences_entity1', 'entity_id_1'),
        Index('idx_entity_cooccurrences_entity2', 'entity_id_2'),
        Index(
            'idx_entity_cooccurrences_count',
            'cooccurrence_count',
            postgresql_ops={'cooccurrence_count': 'DESC'},
        ),
        Index(
            'idx_entity_cooccurrences_temporal',
            'entity_id_1',
            'entity_id_2',
            sql_text('valid_to DESC NULLS FIRST'),
            sql_text('valid_from DESC'),
        ),
    )


class ReflectionStatus(str, Enum):
    """Status of a reflection task in the queue."""

    PENDING = 'pending'
    PROCESSING = 'processing'
    FAILED = 'failed'
    DEAD_LETTER = 'dead_letter'


class BatchJobStatus(str, Enum):
    """Status of a batch ingestion job."""

    PENDING = 'pending'
    PROCESSING = 'processing'
    COMPLETED = 'completed'
    FAILED = 'failed'


class BatchJob(SQLModel, table=True):  # type: ignore
    """
    Queue and status tracker for asynchronous batch ingestion jobs.
    """

    __tablename__ = 'batch_jobs'

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description='Unique identifier for the batch job.',
    )

    vault_id: UUID = vault_id_field()

    status: BatchJobStatus = Field(
        default=BatchJobStatus.PENDING,
        sa_column=Column(Text, nullable=False, server_default=sql_text("'pending'")),
        description='Current status of the batch job.',
    )

    progress: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description='Human-readable progress information.',
    )

    result: dict[str, Any] = Field(
        default={},
        sa_column=Column(JSONB, server_default=sql_text("'{}'::jsonb")),
        description='Final processing results stored as a serialized BatchIngestResponse.',
    )

    notes_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Total number of notes in the batch.',
    )

    processed_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Number of successfully processed notes.',
    )

    skipped_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Number of skipped notes.',
    )

    failed_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Number of failed notes.',
    )

    note_ids: list[str] = Field(
        default=[],
        sa_column=Column(JSONB, server_default=sql_text("'[]'::jsonb")),
        description='List of created Note UUIDs.',
    )

    input_note_keys: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=sql_text("'[]'::jsonb")),
        description=(
            'Sorted, deduped list of NoteInput.calculate_idempotency_key_from_dto '
            'values for the incoming notes. Used by JobManager.create_job to detect '
            'overlap with concurrent pending/processing jobs and return HTTP 409 '
            'instead of starting a duplicate. Set once at row creation; never updated.'
        ),
    )

    error_info: Any | None = Field(
        default=None,
        sa_column=Column(JSONB),
        description='Detailed error information.',
    )

    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()

    started_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),
        description='Timestamp when the job started processing.',
    )

    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True)),
        description='Timestamp when the job finished (success or failure).',
    )

    __table_args__ = (Index('idx_batch_jobs_status', 'status'),)


class ReflectionQueue(SQLModel, table=True):  # type: ignore
    """
    Queue for deferred reflection tasks.
    Tracks which entities need their mental models updated.
    """

    __tablename__ = 'reflection_queue'

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description='Unique identifier for the queue item.',
    )

    entity_id: UUID = Field(
        sa_column=Column(SA_UUID(), ForeignKey('entities.id', ondelete='CASCADE'), nullable=False),
        description='UUID of the entity needing reflection.',
    )

    vault_id: UUID = vault_id_field()

    priority_score: float = Field(
        default=1.0,
        sa_column=Column(Float, nullable=False, server_default='1.0'),
        description='Urgency score calculated from accumulated evidence, graph centrality, and retrieval resonance.',
    )

    accumulated_evidence: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description='Count of new memory units added for this entity since the last reflection.',
    )

    status: ReflectionStatus = Field(
        default=ReflectionStatus.PENDING,
        sa_column=Column(Text, nullable=False, server_default=sql_text("'pending'")),
        description='Current status of the reflection task.',
    )

    last_queued_at: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the entity was last added to or updated in the queue.',
    )

    retry_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description='Number of times this task has been retried after failure.',
    )

    max_retries: int = Field(
        default=3,
        sa_column=Column(Integer, nullable=False, server_default='3'),
        description='Maximum retry attempts before moving to dead letter.',
    )

    last_error: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Error message from the most recent failure.',
    )

    task_type: str = Field(
        default='reflect',
        sa_column=Column(Text, nullable=False, server_default=sql_text("'reflect'")),
        description=(
            "'reflect' = full Phase 0-6 entity reflection; 'refresh_observation' = "
            'surgical re-synthesis of a single observation after MU deprio.'
        ),
    )
    observation_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Refresh-task payload: the observation in mental_models.observations to refresh.',
    )
    priority_lane: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default='false'),
        description='True for refresh tasks and restore-driven priority reflects; claimed ahead of regular tasks.',
    )
    source_unit_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description=(
            'The MU whose deprio triggered the refresh. Used by the post-lock '
            'sibling-query re-check in _refresh_observation.'
        ),
    )

    # Relationships

    entity: 'Entity' = Relationship()

    __table_args__ = (
        Index(
            'idx_reflection_queue_priority',
            'priority_score',
            postgresql_ops={'priority_score': 'DESC'},
        ),
        Index('idx_reflection_queue_status', 'status'),
        CheckConstraint("status IN ('pending', 'processing', 'failed', 'dead_letter')"),
        CheckConstraint(
            "task_type IN ('reflect', 'refresh_observation')",
            name='ck_reflection_queue_task_type',
        ),
        # The three partial indices created in migration 043 are mirrored here
        # so alembic autogenerate does NOT emit drop_index for them on future
        # revisions. Predicate text is rendered by SQLAlchemy in a normalized
        # form; the migration uses raw SQL with literal-string predicates that
        # Postgres canonicalizes to the same shape.
        Index(
            'idx_reflection_queue_lane_priority',
            sql_text('priority_lane DESC'),
            sql_text('priority_score DESC'),
            'last_queued_at',
            postgresql_where=sql_text("status IN ('pending', 'failed')"),
        ),
        Index(
            'idx_reflection_queue_refresh_unique',
            'entity_id',
            'vault_id',
            'observation_id',
            unique=True,
            postgresql_where=sql_text(
                "task_type = 'refresh_observation' AND status IN ('pending', 'processing')"
            ),
        ),
        Index(
            'idx_reflection_queue_entity_vault_active_unique',
            'entity_id',
            'vault_id',
            unique=True,
            postgresql_where=sql_text(
                "task_type = 'reflect' AND status IN ('pending', 'processing')"
            ),
        ),
    )


class MemoryLink(SQLModel, table=True):  # type: ignore
    """
    What it is: A direct relationship between two specific MemoryUnits.
    Function: This creates a chain of thought or a timeline.
    Link Types: The CheckConstraint enforces specific logic:
        - temporal: Unit A happened before Unit B.
        - causes: Unit A caused Unit B.
        - semantic: Unit A is talking about the same topic as Unit B (but maybe in a different document).
    Entity ID (Optional): You can optionally tag a link with an Entity. For example, linking two memories because they both involve "Project X".
    """

    __tablename__ = 'memory_links'

    from_unit_id: UUID = Field(
        primary_key=True,
        sa_column_args=[ForeignKey('memory_units.id', ondelete='CASCADE')],
        description='UUID of the source memory unit.',
    )

    to_unit_id: UUID = Field(
        primary_key=True,
        sa_column_args=[ForeignKey('memory_units.id', ondelete='CASCADE')],
        description='UUID of the target memory unit.',
    )

    vault_id: UUID = vault_id_field()

    link_type: str = Field(
        sa_column=Column(Text, primary_key=True),
        description='Type of the link (e.g., temporal, semantic, causes).',
    )

    entity_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), ForeignKey('entities.id', ondelete='CASCADE')),
        description='Optional UUID of the entity associated with this link.',
    )

    link_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, server_default=sql_text("'{}'::jsonb")),
        description='Structured metadata for the link (e.g., supersession provenance).',
    )

    weight: float = Field(
        default=1.0,
        sa_column=Column(Float, nullable=False, server_default='1.0'),
        description='Strength or certainty of the link (0.0 to 1.0).',
    )

    created_at: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the link was created.',
    )

    from_unit: MemoryUnit = Relationship(
        back_populates='outgoing_links',
        sa_relationship_kwargs={'foreign_keys': '[MemoryLink.from_unit_id]'},
    )
    to_unit: MemoryUnit = Relationship(
        back_populates='incoming_links',
        sa_relationship_kwargs={'foreign_keys': '[MemoryLink.to_unit_id]'},
    )
    entity: Entity | None = Relationship(back_populates='memory_links')

    __table_args__ = (
        CheckConstraint(
            "link_type IN ('temporal', 'semantic', 'entity', 'causes', 'caused_by', 'enables', 'prevents', 'reinforces', 'weakens', 'contradicts', 'refines')",
            name='memory_links_link_type_check',
        ),
        CheckConstraint('weight >= 0.0 AND weight <= 1.0', name='memory_links_weight_check'),
        Index('idx_memory_links_from', 'from_unit_id'),
        Index('idx_memory_links_to', 'to_unit_id'),
        Index('idx_memory_links_type', 'link_type'),
        # Mirrors the migration-side definition in alembic/versions/
        # 033_confidence_evidence_count.py (``_BACKFILL_INDEX_NAME``); name
        # and column order MUST stay in sync with that migration so a fresh
        # ``Base.metadata.create_all`` (test/dev path) lays down the same
        # index that production migrations create.
        Index('idx_memory_links_link_type_to_unit', 'link_type', 'to_unit_id'),
        Index(
            'idx_memory_links_entity',
            'entity_id',
            postgresql_where=sql_text('entity_id IS NOT NULL'),
        ),
        Index(
            'idx_memory_links_from_weight',
            'from_unit_id',
            'weight',
            postgresql_where=sql_text('weight >= 0.1'),
            postgresql_ops={'weight': 'DESC'},
        ),
    )


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


class AuditLog(SQLModel, table=True):  # type: ignore
    """Append-only audit trail for security-relevant events."""

    __tablename__ = 'audit_logs'

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description='Unique identifier for the audit entry.',
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='When the event occurred.',
    )
    actor: str | None = Field(
        default=None,
        max_length=255,
        description='API key identifier or "anonymous" when auth is disabled.',
    )
    action: str = Field(
        max_length=100,
        description='Event type, e.g. auth.success, auth.failure, note.create.',
    )
    resource_type: str | None = Field(
        default=None,
        max_length=100,
        description='Type of resource affected (note, entity, vault, etc.).',
    )
    resource_id: str | None = Field(
        default=None,
        max_length=255,
        description='ID of the affected resource.',
    )
    session_id: str | None = Field(
        default=None,
        max_length=255,
        description='Request session ID for correlation.',
    )
    details: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description='Arbitrary event details (IP, user-agent, etc.).',
    )

    __table_args__ = (
        Index('idx_audit_logs_timestamp', 'timestamp'),
        Index('idx_audit_logs_actor', 'actor'),
        Index('idx_audit_logs_action', 'action'),
        Index('idx_audit_logs_resource', 'resource_type', 'resource_id'),
    )


class OutcomeAuditLog(SQLModel, table=True):  # type: ignore
    """One row per `record_outcome` call.

    Records the per-unit verb payload, coverage stats, and exploration tag
    so signal-quality regressions can be audited offline. Append-only;
    vault-scoped so outcome audit never leaks across tenants.
    """

    __tablename__ = 'outcome_audit_log'

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for this audit row.',
    )
    vault_id: UUID = vault_id_field()
    caller_id: str | None = Field(
        default=None,
        max_length=128,
        sa_column=Column(String(128), nullable=True),
        description='Session id or caller fingerprint (no PII, ≤ 128 chars).',
    )
    units: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False),
        description='Per-unit payload: list of {unit_id, verb, reason?}.',
    )

    @field_validator('units')
    @classmethod
    def _units_must_be_array_of_dicts(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError('OutcomeAuditLog.units must be a list')
        for idx, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(
                    f'OutcomeAuditLog.units[{idx}] must be a dict, got {type(item).__name__}'
                )
        return value

    turn_outcome: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Coarse turn-level outcome label (success/failure/mixed/None).',
    )
    retrieved_set_size: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description='Size of the retrieved set the caller was asked to classify.',
    )
    coverage_ratio: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
        description='reported / retrieved (NULL when retrieved_set_size is unknown).',
    )
    exploration_tagged: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default='false'),
        description='True iff any unit was exploration-injected on retrieval.',
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
        description='Server-side row insertion timestamp.',
    )

    __table_args__ = (
        Index('idx_outcome_audit_log_vault_ts', 'vault_id', sql_text('created_at DESC')),
        Index('idx_outcome_audit_log_caller', 'caller_id'),
        CheckConstraint(
            "jsonb_typeof(units) = 'array'",
            name='outcome_audit_log_units_is_array',
        ),
    )


class KVEntry(SQLModel, table=True):  # type: ignore
    """
    What it is: A key-value store entry scoped by namespace prefix.
    Function: Provides simple, named storage for configuration, preferences,
    and structured data that doesn't fit the note/memory model.
    Key Features:
        - Keys must start with a namespace prefix: global:, user:, project:, or app:.
          Procedures live UNDER a scope namespace as <scope>:procedure:<verb>:<context>
          (bare `procedure:` is no longer a valid top-level namespace — see migration 046).
        - Unique constraint on key.
        - btree index with text_pattern_ops for efficient prefix queries.
        - Optional embedding for semantic search over values (generated from `value`,
          not `key` — key rewrites do not invalidate embeddings).
    """

    __tablename__ = 'kv_entries'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the KV entry.',
    )

    key: str = Field(
        sa_column=Column(Text, nullable=False),
        description=(
            'The key for this entry. Must start with global:, user:, project:, or app:. '
            'Procedures use <scope>:procedure:<verb>:<context>; bare procedure:* is rejected.'
        ),
    )

    value: str = Field(
        sa_column=Column(Text, nullable=False),
        description='The value stored under this key.',
    )

    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(EMBEDDING_DIMENSION)),
        description='Optional embedding vector for semantic search over values.',
    )

    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='When this entry expires. NULL means never.',
    )

    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()

    __table_args__ = (
        UniqueConstraint('key', name='uq_kv_key'),
        Index(
            'idx_kv_key_prefix',
            'key',
            postgresql_using='btree',
            postgresql_ops={'key': 'text_pattern_ops'},
        ),
        Index(
            'idx_kv_expires_at',
            'expires_at',
            postgresql_using='btree',
            postgresql_where=sql_text('expires_at IS NOT NULL'),
        ),
    )


class VaultSummary(SQLModel, table=True):  # type: ignore
    """
    What it is: An evolving summary of what's in a vault.
    Function: Provides a cheap-to-compute overview of vault contents,
    updated incrementally on each note ingestion or regenerated on demand.
    """

    __tablename__ = 'vault_summaries'

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description='Unique identifier for the vault summary.',
    )
    vault_id: UUID = Field(
        sa_column=Column(
            SA_UUID(),
            ForeignKey('vaults.id', ondelete='CASCADE'),
            unique=True,
            nullable=False,
        ),
        description='The vault this summary describes. One summary per vault.',
    )
    narrative: str = Field(
        default='',
        sa_column=Column(Text, server_default=sql_text("''")),
        description='Short thematic synthesis of vault contents (~200 tokens).',
    )
    themes: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, server_default=sql_text("'[]'::jsonb")),
        description=(
            'Extracted themes: [{name, description, note_count, trend, '
            'last_addition, representative_titles}].'
        ),
    )
    inventory: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, server_default=sql_text("'{}'::jsonb")),
        description=(
            'Computed content stats: {total_notes, total_entities, date_range, '
            'by_template, by_source_domain, top_tags, recent_activity}.'
        ),
    )
    key_entities: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, server_default=sql_text("'[]'::jsonb")),
        description='Top entities by mention count: [{name, type, mention_count}].',
    )
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(EMBEDDING_DIMENSION), nullable=True),
        description=(
            'Vector embedding of the narrative, refreshed on every (re)generation. '
            'NULL for empty summaries, pre-migration rows, and encode failures.'
        ),
    )
    version: int = Field(
        default=1,
        description='Incremented on each update (patch or regeneration).',
    )
    notes_incorporated: int = Field(
        default=0,
        description='Count of notes incorporated into this summary.',
    )
    patch_log: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, server_default=sql_text("'[]'::jsonb")),
        description='Last 20 patches: [{note_id, action, timestamp, delta}].',
    )
    needs_regeneration: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=sql_text('false'), nullable=False),
        description=(
            'Set when notes are deleted/archived; triggers full regeneration '
            'on next scheduler cycle.'
        ),
    )
    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()


class NoteAppend(SQLModel, table=True):  # type: ignore
    """Audit row for an atomic delta append to an existing note.

    One row per successful call to the append endpoint, keyed on the
    caller-supplied append_id so retries can replay the cached outcome
    without mutating the body twice.
    """

    __tablename__ = 'note_appends'

    append_id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True),
        description='Caller-supplied idempotency token; primary key.',
    )
    note_id: UUID = Field(
        sa_column=Column(SA_UUID(), nullable=False),
        description='The note this append targeted.',
    )
    delta_sha256: str = Field(
        sa_column=Column(Text, nullable=False),
        description='SHA-256 of the delta bytes; replay-equality check.',
    )
    delta_bytes: int = Field(
        sa_column=Column(Integer, nullable=False),
        description='Length of the delta in bytes (UTF-8 encoded).',
    )
    joiner: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Separator placed between parent body and delta.',
    )
    resulting_content_hash: str = Field(
        sa_column=Column(Text, nullable=False),
        description='content_hash of the note after the append committed.',
    )
    new_unit_ids: list[UUID] = Field(
        default_factory=list,
        sa_column=Column(
            ARRAY(SA_UUID()),
            nullable=False,
            server_default=sql_text('ARRAY[]::uuid[]'),
        ),
        description='Memory units newly extracted from the delta on this append.',
    )
    applied_at: datetime = created_at_field()

    __table_args__ = (
        ForeignKeyConstraint(
            ['note_id'],
            ['notes.id'],
            name='note_appends_note_fkey',
            ondelete='CASCADE',
        ),
        Index('idx_note_appends_note_id_applied_at', 'note_id', 'applied_at'),
    )


# ---------------------------------------------------------------------------
# Procedural plane (procedural memory)
# ---------------------------------------------------------------------------
#
# The procedural plane sits alongside the declarative plane (notes + memory_units).
# It holds exactly two kinds of entity:
#
#   * procedure  — a how-to recipe synthesised from one or more cases
#   * strategy   — an opinionated play-book generalising over procedures
#
# CASES ARE NOT ON THIS PLANE. A case is a note (``notes.role = 'case'``)
# filed into the hidden ``procedural`` system vault via case_submit
# (the design §5.1, §18.3, §18.9.0). Cases feed procedures/strategies as
# lineage via ``procedural_sources``.
#
# Identity is the (kind, scope, verb, context) tuple (UNIQUE NULLS NOT
# DISTINCT): procedure ≡ (scope, verb, context); strategy ≡ (scope, verb,
# NULL) — a strategy is the projection over all procedures sharing
# (scope, verb) (§18.1). Retrieval anchors on the *trigger*
# (when_to_use / when_to_apply): trigger_embedding is the single vector
# leg of the hybrid search; the embedding is recomputed on every trigger
# change to avoid stale-vector drift.
#
# Context-binding pins form a chain global → project:<id> → app:<agent_identity>
# (see spike 7) — the same entry can be pinned at multiple positions of
# different contexts; the (context_key, entry_id) pair is the row key.
# NO ``user`` scope/context: per-user curation rides the pin chain's app
# contexts, not the entries (JG decision 2026-06-10).
# ---------------------------------------------------------------------------


class ProceduralKind(str, Enum):
    """Taxonomy of the procedural plane's two entity kinds.

    Cases are NOT a kind on this plane — a case is a note
    (``notes.role = 'case'``) filed into the hidden ``procedural``
    system vault (the design §18.3 / §18.9.0). Procedures and strategies
    are projections distilled over case clusters.
    """

    PROCEDURE = 'procedure'
    STRATEGY = 'strategy'


class ProceduralStatus(str, Enum):
    """Lifecycle state for an procedural entry.

    * draft        — created but not yet promoted; editable in place.
    * published    — visible to agents via search/briefing.
    * deprecated   — superseded by another entry; kept for lineage.
    """

    DRAFT = 'draft'
    PUBLISHED = 'published'
    DEPRECATED = 'deprecated'


class ProceduralOrigin(str, Enum):
    """How an procedural entry came to exist."""

    SEED = 'seed'  # boot-time system seed
    DERIVED = 'derived'  # LLM-derived from cases (derivation queue)
    MANUAL = 'manual'  # agent-written
    IMPORT = 'import'  # bulk import


class ProceduralSourceRole(str, Enum):
    """Role an procedural_source row plays in a case → procedure relationship."""

    PROVENANCE = 'provenance'  # case that gave rise to a procedure
    EVIDENCE = 'evidence'  # supporting fact for a procedure/strategy
    CONTRADICTION = 'contradiction'  # case that argues against a procedure


class DerivationQueueStatus(str, Enum):
    """State of an entry in the async derivation queue."""

    PENDING = 'pending'
    IN_PROGRESS = 'in_progress'
    COMPLETED = 'completed'
    FAILED = 'failed'


class ProceduralEntry(SQLModel, table=True):  # type: ignore
    """The unit of recall in the procedural plane.

    A procedure or strategy with a stable (kind, scope, verb, context)
    identity anchor (§18.1): procedure ≡ (scope, verb, context);
    strategy ≡ (scope, verb, NULL) — a strategy is the projection over
    all procedures sharing (scope, verb). Retrieval is anchored on the
    *trigger* (when_to_use / when_to_apply): the trigger embedding is the
    vector leg of the hybrid search; the tsvector covers the full text.
    """

    __tablename__ = 'procedural_entries'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the procedural entry.',
    )
    vault_id: UUID = vault_id_field()

    kind: ProceduralKind = Field(
        sa_column=Column(String, nullable=False),
        description='procedure | strategy. Cases are notes (role="case"), not rows here.',
    )
    scope: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Origin scope label: "global", "project:<id>", or '
        '"app:<agent_identity>". NO "user" scope — procedures/strategies are '
        'shared knowledge; per-user briefing customisation lives on the pin '
        'chain, not the entry (JG decision 2026-06-10).',
    )
    verb: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Anchor verb (e.g. "deploy", "migrate"). Required for both kinds.',
    )
    context: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Anchor context (e.g. "nomad", "alembic"). Required for '
        'procedures; MUST be NULL for strategies — a strategy groups all '
        'procedures sharing (scope, verb) (§18.1; see ck_strategy_anchor).',
    )

    title: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Short, imperative title (e.g. "create_alembic_migration").',
    )
    summary: str = Field(
        sa_column=Column(Text, nullable=False),
        description='One-paragraph explanation of when and how to apply this entry.',
    )
    body: str = Field(
        sa_column=Column(Text, nullable=False, server_default=''),
        description='Full procedural body — steps, code, references. '
        'Markdown allowed; rendered as-is by the agent surface.',
    )
    trigger: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='when_to_use (procedure) / when_to_apply (strategy) — '
        'THE retrieval key (spike §19.1: trigger-only embedding beats '
        'full-body 18/20 vs 15/20 top-1). Required at the DTO boundary; '
        'the embedding is recomputed on every trigger change.',
    )
    trigger_embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(EMBEDDING_DIMENSION), nullable=True),
        description='Vector(384) embedding of `trigger` — the single '
        'vector leg of the hybrid search. Recomputed on every trigger change.',
    )

    tags: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text), nullable=False, server_default=sql_text('ARRAY[]::text[]')),
        description='Free-form tags — domain, sub-system, framework.',
    )
    extra_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column('metadata', JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")),
        description='Arbitrary metadata — confidence, run_count, last_verified_at, etc.',
    )

    status: ProceduralStatus = Field(
        default=ProceduralStatus.DRAFT,
        sa_column=Column(String, nullable=False, server_default=ProceduralStatus.DRAFT.value),
        description='Lifecycle state. Draft entries are not visible to search/briefing.',
    )
    origin: ProceduralOrigin = Field(
        default=ProceduralOrigin.MANUAL,
        sa_column=Column(String, nullable=False, server_default=ProceduralOrigin.MANUAL.value),
        description='How this entry came to exist; used by the audit/replay surface.',
    )

    supersedes_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Entry this one supersedes (lineage).',
    )
    superseded_by_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Entry that supersedes this one (lineage).',
    )

    search_tsvector: Any = Field(
        default=None,
        sa_column=Column(
            TSVECTOR,
            Computed(
                "to_tsvector('english'::regconfig, "
                "coalesce(title, '') || ' ' || "
                "coalesce(summary, '') || ' ' || "
                "coalesce(trigger, '') || ' ' || "
                "coalesce(body, '') || ' ' || "
                "coalesce(memex_procedural_tags_to_text(tags), ''))",
                persisted=True,
            ),
        ),
        description='Generated tsvector over title + summary + trigger + body + tags.',
    )

    created_at: datetime = created_at_field()
    updated_at: datetime = updated_at_field()
    published_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='Set when status transitions draft → published.',
    )

    __table_args__ = (
        # Identity anchor for procedures and strategies.
        # NULLS NOT DISTINCT means (kind, 'proc', 'migrate', 'postgres') is unique
        # even though verb/context are NULLable for cases.
        UniqueConstraint(
            'kind',
            'scope',
            'verb',
            'context',
            name='uq_procedural_identity',
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "kind IN ('procedure', 'strategy')",
            name='ck_procedural_kind',
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'deprecated')",
            name='ck_procedural_status',
        ),
        CheckConstraint(
            "origin IN ('seed', 'derived', 'manual', 'import')",
            name='ck_procedural_origin',
        ),
        # Anchor shapes (§18.1): procedure ≡ (scope, verb, context);
        # strategy ≡ (scope, verb, NULL) — a strategy groups all
        # procedures sharing (scope, verb), so its context MUST be NULL.
        CheckConstraint(
            "kind <> 'strategy' OR (verb IS NOT NULL AND context IS NULL)",
            name='ck_strategy_anchor',
        ),
        CheckConstraint(
            "kind <> 'procedure' OR (verb IS NOT NULL AND context IS NOT NULL)",
            name='ck_procedure_anchor',
        ),
        # NOTE: there is intentionally NO `(trigger IS NULL) = (trigger_embedding IS NULL)`
        # CHECK. The embedding is computed by the caller at write time
        # (§18.7 — the facade embeds the trigger and threads the vector
        # into the repository), but an embedder outage degrades to a
        # NULL vector (BM25 still covers the row) rather than failing
        # the write — a pairing CHECK would turn that graceful
        # degradation into a constraint violation.
        ForeignKeyConstraint(
            ['supersedes_id'],
            ['procedural_entries.id'],
            name='procedural_entries_supersedes_fkey',
            ondelete='SET NULL',
        ),
        ForeignKeyConstraint(
            ['superseded_by_id'],
            ['procedural_entries.id'],
            name='procedural_entries_superseded_by_fkey',
            ondelete='SET NULL',
        ),
        Index('idx_procedural_entries_vault_kind', 'vault_id', 'kind'),
        Index(
            'idx_procedural_entries_vault_status',
            'vault_id',
            'status',
        ),
        Index(
            'idx_procedural_entries_scope_verb',
            'scope',
            'verb',
            postgresql_where=sql_text("kind IN ('procedure', 'strategy')"),
        ),
        Index(
            'idx_procedural_entries_status_published_at',
            'status',
            'published_at',
            postgresql_ops={'published_at': 'DESC'},
            postgresql_where=sql_text("status = 'published'"),
        ),
        Index(
            'idx_procedural_entries_trigger_embedding',
            'trigger_embedding',
            postgresql_using='hnsw',
            postgresql_ops={'trigger_embedding': 'vector_cosine_ops'},
            postgresql_where=sql_text("status = 'published'"),
        ),
        Index(
            'idx_procedural_entries_search_tsvector',
            'search_tsvector',
            postgresql_using='gin',
        ),
    )


class ProceduralEntryVersion(SQLModel, table=True):  # type: ignore
    """Append-only version ledger for an procedural entry.

    A row per `edit` call; the latest body is always on ``procedural_entries``
    (mutable in place — see the design §3.2). ``version`` is monotonically
    increasing per entry_id.
    """

    __tablename__ = 'procedural_entry_versions'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for this version row.',
    )
    entry_id: UUID = Field(
        sa_column=Column(SA_UUID(), nullable=False),
        description='The entry this version belongs to.',
    )
    version: int = Field(
        sa_column=Column(Integer, nullable=False),
        description='Monotonic version per entry_id (1, 2, 3, …).',
    )
    title: str = Field(sa_column=Column(Text, nullable=False))
    summary: str = Field(sa_column=Column(Text, nullable=False))
    body: str = Field(sa_column=Column(Text, nullable=False, server_default=''))
    trigger: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    tags: list[str] = Field(
        default_factory=list,
        sa_column=Column(ARRAY(Text), nullable=False, server_default=sql_text('ARRAY[]::text[]')),
    )
    extra_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column('metadata', JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")),
    )
    edited_by: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Agent identity or "system" that produced this version.',
    )
    edit_reason: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Free-form explanation of why the edit was made.',
    )
    created_at: datetime = created_at_field()

    __table_args__ = (
        ForeignKeyConstraint(
            ['entry_id'],
            ['procedural_entries.id'],
            name='procedural_entry_versions_entry_fkey',
            ondelete='CASCADE',
        ),
        UniqueConstraint('entry_id', 'version', name='uq_procedural_entry_versions_entry_version'),
        Index('idx_procedural_entry_versions_entry_id_created_at', 'entry_id', 'created_at'),
    )


class ProceduralSource(SQLModel, table=True):  # type: ignore
    """Provenance + evidence + contradiction edges between procedural entries.

    For a case → procedure edge the role is ``provenance``. For a procedure →
    evidence-fact edge the role is ``evidence``. For a case that argues
    against a procedure the role is ``contradiction``.
    """

    __tablename__ = 'procedural_sources'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for this source edge.',
    )
    entry_id: UUID = Field(
        sa_column=Column(SA_UUID(), nullable=False),
        description='The procedure/strategy that draws on the source.',
    )
    source_entry_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='The case (or other entry) that is being cited.',
    )
    source_note_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Optional declarative-plane note backing this edge.',
    )
    source_memory_unit_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Optional memory unit backing this edge.',
    )
    role: ProceduralSourceRole = Field(
        sa_column=Column(String, nullable=False),
        description='provenance | evidence | contradiction.',
    )
    weight: float = Field(
        default=1.0,
        sa_column=Column(Float, nullable=False, server_default='1.0'),
        description='Edge weight used in RRF aggregation; default 1.0.',
    )
    created_at: datetime = created_at_field()

    __table_args__ = (
        CheckConstraint(
            "role IN ('provenance', 'evidence', 'contradiction')",
            name='ck_procedural_sources_role',
        ),
        CheckConstraint(
            'weight >= 0.0 AND weight <= 10.0',
            name='ck_procedural_sources_weight',
        ),
        ForeignKeyConstraint(
            ['entry_id'],
            ['procedural_entries.id'],
            name='procedural_sources_entry_fkey',
            ondelete='CASCADE',
        ),
        ForeignKeyConstraint(
            ['source_entry_id'],
            ['procedural_entries.id'],
            name='procedural_sources_source_entry_fkey',
            ondelete='CASCADE',
        ),
        ForeignKeyConstraint(
            ['source_note_id'],
            ['notes.id'],
            name='procedural_sources_source_note_fkey',
            ondelete='SET NULL',
        ),
        ForeignKeyConstraint(
            ['source_memory_unit_id'],
            ['memory_units.id'],
            name='procedural_sources_source_memory_unit_fkey',
            ondelete='SET NULL',
        ),
        # At least one source pointer must be set.
        CheckConstraint(
            'source_entry_id IS NOT NULL OR source_note_id IS NOT NULL OR '
            'source_memory_unit_id IS NOT NULL',
            name='ck_procedural_sources_pointer_set',
        ),
        Index('idx_procedural_sources_entry_id', 'entry_id'),
        Index('idx_procedural_sources_source_entry_id', 'source_entry_id'),
    )


class ProceduralPin(SQLModel, table=True):  # type: ignore
    """Context-binding pin: a (context_key, entry_id, position) triple.

    Pins form a chain ``global → project:<id> → app:<agent_identity>`` (see
    spike 7). The same entry can be pinned at multiple positions of
    different contexts; the (context_key, entry_id, position) triple is the
    row key. Lower position = higher priority; agents read pins in ascending
    position order.
    """

    __tablename__ = 'procedural_pins'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for this pin row.',
    )
    context_key: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Binding key — "global", "project:<uuid>", or '
        '"app:<agent_identity>". Chain is implicit: agents read all three.',
    )
    entry_id: UUID = Field(
        sa_column=Column(SA_UUID(), nullable=False),
        description='The entry being pinned.',
    )
    position: int = Field(
        sa_column=Column(Integer, nullable=False),
        description='Position within the pin list for this context_key. '
        'Lower = higher priority. 0-based.',
    )
    pinned_by: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Agent identity or "system" that created the pin.',
    )
    created_at: datetime = created_at_field()

    __table_args__ = (
        ForeignKeyConstraint(
            ['entry_id'],
            ['procedural_entries.id'],
            name='procedural_pins_entry_fkey',
            ondelete='CASCADE',
        ),
        # One pin per (context, entry) — prevents double-pinning the
        # same entry into a context (which would render duplicate
        # briefing cards and burn the per-context budget). Position is
        # append-computed (max+1), so it's naturally unique without a
        # constraint of its own.
        UniqueConstraint(
            'context_key',
            'entry_id',
            name='uq_procedural_pins_context_entry',
        ),
        CheckConstraint('position >= 0', name='ck_procedural_pins_position_nonneg'),
        Index(
            'idx_procedural_pins_context_position',
            'context_key',
            'position',
        ),
    )


class ProceduralDerivationQueue(SQLModel, table=True):  # type: ignore
    """Async derivation queue: cases in, procedures/strategies out.

    Workers claim rows via ``SELECT ... FOR UPDATE SKIP LOCKED`` — same
    leader-election-free pattern the reflection queue uses (see
    the design §4.4).
    """

    __tablename__ = 'procedural_derivation_queue'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for this queue row.',
    )
    vault_id: UUID = vault_id_field()
    source_entry_ids: list[UUID] = Field(
        sa_column=Column(
            ARRAY(SA_UUID()),
            nullable=False,
            server_default=sql_text('ARRAY[]::uuid[]'),
        ),
        description='Cases that should be distilled into a procedure/strategy.',
    )
    target_kind: ProceduralKind = Field(
        sa_column=Column(String, nullable=False),
        description='What to derive — procedure or strategy.',
    )
    target_scope: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Scope label for the derived entry (e.g. "global", "project:<uuid>").',
    )
    target_verb: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    target_context: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status: DerivationQueueStatus = Field(
        default=DerivationQueueStatus.PENDING,
        sa_column=Column(
            String,
            nullable=False,
            server_default=DerivationQueueStatus.PENDING.value,
        ),
        description='Queue state — pending → in_progress → completed | failed.',
    )
    attempt_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description='Worker attempts so far. Bounded by config.derivation_max_attempts.',
    )
    last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    result_entry_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Set when status=completed; the derived entry.',
    )
    claimed_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='Last worker-claim timestamp.',
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
    )
    created_at: datetime = created_at_field()

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('procedure', 'strategy')",
            name='ck_derivation_queue_target_kind',
        ),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'failed')",
            name='ck_derivation_queue_status',
        ),
        CheckConstraint('attempt_count >= 0', name='ck_derivation_queue_attempt_nonneg'),
        # Strategy derivations anchor on (scope, verb) — verb required,
        # context FORBIDDEN (mirrors ck_strategy_anchor on the entries
        # table). The old "context required" rule was backwards: it made
        # every strategy enqueue impossible (entry.context is NULL for
        # strategies), so the dirty-event enqueue silently never fired.
        CheckConstraint(
            "target_kind <> 'strategy' OR (target_verb IS NOT NULL AND target_context IS NULL)",
            name='ck_derivation_queue_strategy_anchor',
        ),
        Index(
            'idx_derivation_queue_status_created_at',
            'status',
            'created_at',
            postgresql_where=sql_text("status = 'pending'"),
        ),
        Index('idx_derivation_queue_vault_id', 'vault_id'),
    )


# ---------------------------------------------------------------------------
# Maintenance ledger (rule-based linter)
# ---------------------------------------------------------------------------


class LintType(str, Enum):
    STRUCTURAL = 'structural'
    QUALITY = 'quality'
    GOVERNANCE = 'governance'
    SCHEMA = 'schema'
    ROUTING = 'routing'


class LintStatus(str, Enum):
    PENDING = 'pending'
    RESOLVED = 'resolved'
    DISMISSED = 'dismissed'


class LintSource(str, Enum):
    RULE = 'rule'
    LLM = 'llm'
    EXTERNAL = 'external'


class MaintenanceProposal(SQLModel, table=True):  # type: ignore
    """Finding ledger row emitted by the LintService.

    Read-only from the agent surface. The unique partial index on
    ``(rule_name, target_type, target_id, vault_id) WHERE status = 'pending'``
    makes ``LintService.run_rules`` idempotent on reruns. ``vault_id`` is
    nullable per acceptance criteria (NULL = global findings; reserved for Tier B).
    """

    __tablename__ = 'maintenance_proposals'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the maintenance proposal.',
    )
    vault_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            SA_UUID(),
            ForeignKey('vaults.id', ondelete='CASCADE'),
            nullable=True,
        ),
        description='Vault this finding belongs to. NULL = global; reserved for Tier B.',
    )
    lint_type: LintType = Field(
        sa_column=Column(Text, nullable=False),
        description='Category of the finding.',
    )
    target_type: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Type of the targeted entity (e.g. 'memory_unit', 'mental_model').",
    )
    target_id: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Opaque identifier of the targeted entity.',
    )
    rule_name: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Name of the rule that emitted this finding.',
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb")),
        description='Rule-specific payload describing why the finding fired.',
    )
    suggested_action: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Free-text suggestion for the agent or operator.',
    )
    status: LintStatus = Field(
        default=LintStatus.PENDING,
        sa_column=Column(Text, nullable=False, server_default=sql_text("'pending'")),
        description='Lifecycle state of the finding.',
    )
    source: LintSource = Field(
        default=LintSource.RULE,
        sa_column=Column(Text, nullable=False, server_default=sql_text("'rule'")),
        description='Whether the finding came from a SQL rule, an LLM check, or an external tool.',
    )
    created_at: datetime = created_at_field()
    resolved_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='Set when status flips to resolved or dismissed.',
    )
    resolved_by: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description=(
            'Free-text actor that resolved the proposal (e.g. agent name '
            'or operator id). NULL while the proposal is pending; set '
            'alongside resolved_at when status flips to resolved/dismissed.'
        ),
    )
    flagged_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='Set when the finding is flagged for later review; NULL when unflagged.',
    )

    __table_args__ = (
        CheckConstraint(
            "lint_type IN ('structural', 'quality', 'governance', 'schema', 'routing')",
            name='ck_maintenance_proposals_lint_type',
        ),
        CheckConstraint(
            "status IN ('pending', 'resolved', 'dismissed')",
            name='ck_maintenance_proposals_status',
        ),
        CheckConstraint(
            "source IN ('rule', 'llm', 'external')",
            name='ck_maintenance_proposals_source',
        ),
        Index('idx_maintenance_proposals_vault_status', 'vault_id', 'status'),
        Index('idx_maintenance_proposals_lint_type', 'lint_type'),
        Index(
            'uq_maintenance_proposals_pending',
            'rule_name',
            'target_type',
            'target_id',
            'vault_id',
            unique=True,
            postgresql_where=sql_text("status = 'pending'"),
        ),
    )


class ConsolidationTick(SQLModel, table=True):  # type: ignore
    """One row per ``consolidation_tick(vault_id)`` invocation.

    ``services/consolidation.py`` is a thin orchestrator over
    reflection + contradiction + prune-stale-only; this row is its sole
    DB write at the end of each tick (acceptance criteria). ``completed_at IS NULL``
    signals an in-progress tick; the gap between ``started_at`` and
    ``completed_at`` is wall-clock duration.
    """

    __tablename__ = 'consolidation_ticks'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the tick row.',
    )

    vault_id: UUID = Field(
        sa_column=Column(
            SA_UUID(),
            nullable=False,
        ),
        description='Vault this tick consolidated (vault-scoping invariant: NOT NULL).',
    )

    started_at: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), nullable=False),
        description='Wall-clock timestamp when the tick began.',
    )

    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='Wall-clock timestamp when the tick finished; NULL means in-progress.',
    )

    units_processed: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=sql_text('0')),
        description='Memory units returned by select_diff_units (capped at 500/tick).',
    )

    entities_reflected: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=sql_text('0')),
        description='Distinct entities passed to ReflectionService during this tick.',
    )

    contradictions_run: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=sql_text('0')),
        description='Contradiction-detection invocations made during this tick.',
    )

    stale_pruned: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=sql_text('0')),
        description='Units pruned by prune_stale_evidence (status=STALE only).',
    )

    error: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Free-text error message on failure; None on success.',
    )

    created_at: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
        description='Timestamp when the row was inserted.',
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ['vault_id'],
            ['vaults.id'],
            ondelete='CASCADE',
            name='fk_consolidation_ticks_vault_id',
        ),
        Index('idx_consolidation_ticks_vault_started', 'vault_id', 'started_at'),
        Index(
            'idx_consolidation_ticks_vault_completed',
            'vault_id',
            sql_text('completed_at DESC NULLS LAST'),
        ),
    )


# ---------------------------------------------------------------------------
# LLM lint quota (rolling-24h cost cap)
# ---------------------------------------------------------------------------


class LintRuleTelemetry(SQLModel, table=True):  # type: ignore
    """Rolled-up per-rule verdict counters — feeds the auto-learning loop.

    Layer 2 of the lint auto-learning architecture: nightly (or on-demand)
    the ``LintLearningService`` reads resolved ``maintenance_proposals`` for a
    trailing window and writes one row here per ``(rule_name, vault_id)``.
    ``vault_id IS NULL`` means a global rollup across vaults.

    Layers 3 and 4 (threshold calibration, DSPy compile) read this table to
    decide whether they have enough labelled data to act. ``memex lint stats``
    renders it directly.
    """

    __tablename__ = 'lint_rule_telemetry'

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(
            SA_UUID(),
            primary_key=True,
            server_default=sql_text('gen_random_uuid()'),
        ),
    )
    rule_name: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Rule that produced the verdicts in this rollup.',
    )
    vault_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Vault scope; NULL = global rollup across vaults.',
    )
    window_start: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), nullable=False),
        description='Start of the rolling window this row aggregates (inclusive).',
    )
    window_end: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), nullable=False),
        description='End of the rolling window this row aggregates (exclusive).',
    )
    accept_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description=(
            'Verdicts where a canned action ran (resolution.followup.action set, '
            "action_id != 'no_op'). Counts as 'the rule's signal was useful'."
        ),
    )
    no_op_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description=(
            'Verdicts where the operator chose no_op — they reviewed the proposal '
            'and chose not to mutate state. The rule was valid but did not warrant '
            'action this time.'
        ),
    )
    dismiss_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description='Verdicts where the operator dismissed the proposal as noise.',
    )
    legacy_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default='0'),
        description=(
            'Pre-cockpit rows: resolved without a resolution.followup block. '
            'Cannot be classified as accept / no_op; counted separately so '
            'operators can see how much history is unlabelled.'
        ),
    )
    median_surprise: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
        description='Median evidence.surprise_score across rows in the window.',
    )
    median_time_to_resolve_seconds: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description=(
            'Median seconds between created_at and resolved_at. Slow verdicts are '
            'higher-signal for later learning phases.'
        ),
    )
    refreshed_at: datetime = Field(
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=sql_text('now()'),
            nullable=False,
        ),
        description='When this row was last (re)written by the rollup service.',
    )

    __table_args__ = (
        UniqueConstraint(
            'rule_name',
            'vault_id',
            'window_start',
            name='uq_lint_rule_telemetry_rule_vault_window',
        ),
        Index('idx_lint_rule_telemetry_rule_window', 'rule_name', 'window_end'),
        Index('idx_lint_rule_telemetry_vault_window', 'vault_id', 'window_end'),
    )


class LintLLMQuota(SQLModel, table=True):  # type: ignore
    """Hour-bucket counter for the 24h-rolling cost cap.

    One row per (vault_id, hour_bucket). The 24h rolling window is computed by
    summing the last 24 hour-buckets via the indexed range scan
    ``idx_lint_llm_quota_vault_hour``. UPSERT is idempotent through the
    ``uq_lint_llm_quota_vault_hour`` constraint.
    """

    __tablename__ = 'lint_llm_quota'

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(
            SA_UUID(),
            primary_key=True,
            server_default=sql_text('gen_random_uuid()'),
        ),
    )
    vault_id: UUID = Field(
        sa_column=Column(
            SA_UUID(),
            ForeignKey('vaults.id', ondelete='CASCADE'),
            nullable=False,
        ),
        description='Vault this quota row belongs to.',
    )
    hour_bucket: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), nullable=False),
        description='UTC timestamp truncated to the hour. Clients MUST normalise.',
    )
    count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=sql_text('0')),
        description='Number of LLM lint calls made in this hour for this vault.',
    )

    __table_args__ = (
        UniqueConstraint('vault_id', 'hour_bucket', name='uq_lint_llm_quota_vault_hour'),
        CheckConstraint('count >= 0', name='ck_lint_llm_quota_count_non_negative'),
        Index('idx_lint_llm_quota_vault_hour', 'vault_id', 'hour_bucket'),
    )


class LintRuleCalibration(SQLModel, table=True):  # type: ignore
    """Versioned per-rule emission thresholds learned from operator verdicts.

    Layer 3 of the lint auto-learning architecture. The calibration job reads
    ``lint_rule_telemetry`` (Layer 2), applies an accept-rate rule, and writes
    a new row here. LLM checks read the latest unsuperseded row per
    ``(rule_name, vault_id)`` at emission time.
    """

    __tablename__ = 'lint_rule_calibration'

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(
            SA_UUID(),
            primary_key=True,
            server_default=sql_text('gen_random_uuid()'),
        ),
    )
    rule_name: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Rule whose thresholds are calibrated.',
    )
    vault_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Vault scope; NULL = global calibration.',
    )
    version: int = Field(
        sa_column=Column(Integer, nullable=False),
        description='Monotonically increasing version within (rule_name, vault_id).',
    )
    surprise_threshold: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
        description='Learned surprise_score emission threshold.',
    )
    polarity_threshold: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
        description='Learned polarity emission threshold.',
    )
    learned_at: datetime = Field(
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=sql_text('now()'),
            nullable=False,
        ),
        description='When this calibration row was written.',
    )
    learned_from_window_start: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='Start of the telemetry window this row was derived from.',
    )
    learned_from_window_end: datetime | None = Field(
        default=None,
        sa_column=Column(TIMESTAMP(timezone=True), nullable=True),
        description='End of the telemetry window this row was derived from.',
    )
    # Three states: NULL (active), positive int (superseded by that
    # version), -1 (rolled back by operator).
    superseded_by_version: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description='Version that superseded this row; NULL = active.',
    )
    frozen: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default='false', nullable=False),
        description='When true, auto-calibration skips this rule.',
    )
    rationale: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description='JSON blob explaining the calibration decision.',
    )

    __table_args__ = (
        UniqueConstraint(
            'rule_name',
            'vault_id',
            'version',
            name='uq_lint_calibration_rule_vault_version',
        ),
        Index(
            'idx_lint_calibration_active',
            'rule_name',
            'vault_id',
            postgresql_where=sql_text('superseded_by_version IS NULL'),
        ),
        CheckConstraint(
            'superseded_by_version IS NULL OR superseded_by_version = -1'
            ' OR superseded_by_version > 0',
            name='ck_lint_rule_calibration_superseded_valid',
        ),
    )


class LintLLMSignature(SQLModel, table=True):  # type: ignore
    """Versioned compiled DSPy signatures for LLM lint checks.

    Layer 4 of the lint auto-learning architecture. The optimizer reads
    labelled verdicts, compiles a DSPy signature, validates against the
    champion, and promotes the winner here. LLM checks load the latest
    unsuperseded row per ``(rule_name, vault_id)`` at server startup.
    """

    __tablename__ = 'lint_llm_signature'

    id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(
            SA_UUID(),
            primary_key=True,
            server_default=sql_text('gen_random_uuid()'),
        ),
    )
    rule_name: str = Field(
        sa_column=Column(Text, nullable=False),
        description='Rule this signature was compiled for.',
    )
    vault_id: UUID | None = Field(
        default=None,
        sa_column=Column(SA_UUID(), nullable=True),
        description='Vault scope; NULL = global signature.',
    )
    version: int = Field(
        sa_column=Column(Integer, nullable=False),
        description='Monotonically increasing version within (rule_name, vault_id).',
    )
    compiled_program: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description='Serialised compiled DSPy program.',
    )
    demos: list[dict[str, Any]] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description='Serialised few-shot demos from the bootstrap.',
    )
    base_model: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='LM identifier used during compilation.',
    )
    validation_score: float | None = Field(
        default=None,
        sa_column=Column(Float, nullable=True),
        description='Accuracy on the temporal validation split.',
    )
    validation_examples: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description='Number of examples in the validation split.',
    )
    promoted_at: datetime = Field(
        sa_column=Column(
            TIMESTAMP(timezone=True),
            server_default=sql_text('now()'),
            nullable=False,
        ),
        description='When this signature was promoted.',
    )
    promoted_by: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description='Actor that promoted this signature.',
    )
    # Three states: NULL (active), positive int (superseded by that
    # version), -1 (rolled back by operator).
    superseded_by_version: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description='Version that superseded this row; NULL = active.',
    )

    __table_args__ = (
        UniqueConstraint(
            'rule_name',
            'vault_id',
            'version',
            name='uq_lint_llm_signature_rule_vault_version',
        ),
        Index(
            'idx_lint_llm_signature_active',
            'rule_name',
            'vault_id',
            postgresql_where=sql_text('superseded_by_version IS NULL'),
        ),
        CheckConstraint(
            'superseded_by_version IS NULL OR superseded_by_version = -1'
            ' OR superseded_by_version > 0',
            name='ck_lint_llm_signature_superseded_valid',
        ),
    )


# ---------------------------------------------------------------------------
# DDL event hooks
# ---------------------------------------------------------------------------
#
# The procedural_entries.search_tsvector generated column references
# memex_procedural_tags_to_text() — an IMMUTABLE wrapper around the
# STABLE array_to_string(text[], text). Migration 061 creates the function
# in upgrade(); that path is fine for alembic-driven schema setup.
#
# Tests that drive schema setup via ``SQLModel.metadata.create_all``
# (notably the integration ``engine`` fixture in
# packages/core/tests/integration/conftest.py) never run alembic, so the
# function would not exist when ``create_all`` reaches the
# ``procedural_entries`` table. To keep both paths consistent we
# register a ``before_create`` listener that emits the function DDL
# before any table DDL, mirroring the migration body.
# ---------------------------------------------------------------------------


_PROCEDURAL_TAGS_TO_TEXT_DDL = """
CREATE OR REPLACE FUNCTION memex_procedural_tags_to_text(
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


@event.listens_for(SQLModel.metadata, 'before_create', propagate=True)
def _emit_procedural_tags_to_text(target, connection, **kwargs):  # type: ignore[no-untyped-def]
    """Emit the procedural tsvector helper before any table that uses it.

    Mirrors the upgrade body of migration 061 — keeps ``create_all`` and
    ``alembic upgrade`` consistent. Idempotent (``CREATE OR REPLACE``)
    so re-running setup is safe.
    """
    if not connection.dialect.name.startswith('postgresql'):
        return
    connection.exec_driver_sql(_PROCEDURAL_TAGS_TO_TEXT_DDL)
