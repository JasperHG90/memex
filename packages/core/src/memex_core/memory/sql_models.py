from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    Computed,
    ForeignKey,
    Integer,
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
from sqlalchemy.types import Uuid as SA_UUID
from sqlmodel import SQLModel, Field, Relationship

from memex_core.context import get_session_id
from memex_core.memory.mixins import vault_id_field, created_at_field, updated_at_field

from memex_common.schemas import MemoryUnitBase, FactTypes

EMBEDDING_DIMENSION = 384


class ContentStatus(str, Enum):
    """Status of content units (chunks, memory units)."""

    ACTIVE = 'active'
    STALE = 'stale'


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
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp when the vault was created.',
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

    __table_args__ = (
        # Enforce uniqueness for Entity + Vault (Global or Specific)
        Index(
            'idx_mental_models_entity_vault_unique',
            'entity_id',
            'vault_id',
            unique=True,
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
        description='Note lifecycle status: active, superseded, appended, archived.',
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

    summary_version_incorporated: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description='VaultSummary.version when this note was last incorporated into the summary. '
        'NULL or < current version means pending.',
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
            "status IN ('active', 'superseded', 'appended', 'archived')",
            name='ck_notes_status',
        ),
        Index(
            'idx_notes_title_trgm',
            sql_text('lower(title) gin_trgm_ops'),
            postgresql_using='gin',
        ),
        Index('idx_notes_summary_version', 'vault_id', 'summary_version_incorporated'),
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

    access_count: int = Field(
        default=0,
        sa_column=Column(Integer, server_default='0'),
        description='Number of times the memory unit has been accessed.',
    )

    confidence: float = Field(
        default=1.0,
        sa_column=Column(Float, nullable=False, server_default='1.0'),
        description='Confidence score (0.0-1.0). Decreased when contradicted by newer information.',
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
            'confidence >= 0.0 AND confidence <= 1.0',
            name='memory_units_confidence_check',
        ),
        Index('idx_memory_units_note_id', 'note_id'),
        Index('idx_memory_units_chunk_id', 'chunk_id'),
        Index('idx_memory_units_status', 'status'),
        Index('idx_memory_units_event_date', 'event_date', postgresql_ops={'event_date': 'DESC'}),
        Index(
            'idx_memory_units_access_count', 'access_count', postgresql_ops={'access_count': 'DESC'}
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

    vault_id: UUID = vault_id_field()

    cooccurrence_count: int = Field(
        default=1,
        sa_column=Column(Integer, server_default='1'),
        description='Number of times these two entities have appeared together.',
    )

    last_cooccurred: datetime = Field(
        sa_column=Column(TIMESTAMP(timezone=True), server_default=func.now()),
        description='Timestamp of the most recent cooccurrence.',
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
        sa_column=Column(Text, nullable=False, server_default='pending'),
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
        sa_column=Column(Text, nullable=False, server_default='pending'),
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
        # Ensure only one pending/processing task per entity per vault
        # Note: Standard SQL UNIQUE considers NULLs distinct.
        # We rely on the application layer (ReflectionQueueService) to handle the logic for global (NULL) vault uniqueness,
        # or we could use a partial index if strictly necessary.
        # For now, a composite index helps lookups.
        Index('idx_reflection_queue_entity_vault', 'entity_id', 'vault_id'),
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
            "link_type IN ('temporal', 'semantic', 'entity', 'causes', 'caused_by', 'enables', 'prevents', 'reinforces', 'weakens', 'contradicts')",
            name='memory_links_link_type_check',
        ),
        CheckConstraint('weight >= 0.0 AND weight <= 1.0', name='memory_links_weight_check'),
        Index('idx_memory_links_from', 'from_unit_id'),
        Index('idx_memory_links_to', 'to_unit_id'),
        Index('idx_memory_links_type', 'link_type'),
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


class KVEntry(SQLModel, table=True):  # type: ignore
    """
    What it is: A key-value store entry scoped by namespace prefix.
    Function: Provides simple, named storage for configuration, preferences,
    and structured data that doesn't fit the note/memory model.
    Key Features:
        - Keys must start with a namespace prefix: global:, user:, project:, or app:.
        - Unique constraint on key.
        - btree index with text_pattern_ops for efficient prefix queries.
        - Optional embedding for semantic search over values.
    """

    __tablename__ = 'kv_entries'

    id: UUID = Field(
        sa_column=Column(SA_UUID(), primary_key=True, server_default=sql_text('gen_random_uuid()')),
        description='Unique identifier for the KV entry.',
    )

    key: str = Field(
        sa_column=Column(Text, nullable=False),
        description='The key for this entry. Must start with global:, user:, project:, or app:.',
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
