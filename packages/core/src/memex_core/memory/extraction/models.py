import datetime as dt
import hashlib
import json
import re
import uuid
from typing import Literal, Any
from dataclasses import dataclass

from uuid import UUID

import tiktoken
from sqlmodel import SQLModel, Field
from pydantic import BaseModel, field_serializer, field_validator

from memex_core.types import CausalRelationshipTypes, FactTypes, FactKindTypes
from memex_core.config import GLOBAL_VAULT_ID

DATE_REGEX = r'^\d{4}-\d{2}-\d{2}$'

_ENCODER = tiktoken.get_encoding('cl100k_base')


def estimate_token_count(text: str | None) -> int:
    """Estimate the token count of a text string using cl100k_base encoding."""
    if not text:
        return 0
    return len(_ENCODER.encode(text))


def content_hash_md5(text: str) -> str:
    """MD5 hash of text content, used for node-level identity."""
    return hashlib.md5(text.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# PageIndex models (ported from prototype)
# ---------------------------------------------------------------------------


class SectionSummary(BaseModel):
    """Structured 5W summary of a document section."""

    who: str | None = Field(None, description='Entities, people, or systems involved.')
    what: str | None = Field(None, description='Core events, topics, or actions discussed.')
    how: str | None = Field(None, description='Methods, processes, or mechanisms described.')
    when: str | None = Field(None, description='Timeframes, dates, or sequences mentioned.')
    where: str | None = Field(None, description='Locations, contexts, or environments.')

    @property
    def formatted(self) -> str:
        parts: list[str] = []
        if self.who:
            parts.append(self.who)
        if self.what:
            parts.append(self.what)
        if self.how:
            parts.append(self.how)
        if self.when:
            parts.append(self.when)
        if self.where:
            parts.append(self.where)
        return ' | '.join(parts)


class BlockSummary(BaseModel):
    """Synthesized summary of a block composed of multiple sections."""

    topic: str = Field(..., description='The overarching topic or theme of this block.')
    key_points: list[str] = Field(
        default_factory=list,
        description='3-5 key points covered in this block. '
        'Each point should be a complete sentence WITHOUT trailing punctuation.',
    )

    @property
    def formatted(self) -> str:
        if not self.key_points:
            return self.topic
        points = ' | '.join(self.key_points)
        return f'{self.topic} — {points}'


class DetectedHeader(BaseModel):
    """A header detected in the document text (by regex or LLM scanning)."""

    reasoning: str = Field(
        ...,
        description='Explain WHY this line is a header.',
    )
    exact_text: str = Field(..., description='The EXACT string sequence found in the source text.')
    clean_title: str = Field(..., description='The cleaned, human-readable title.')
    level_hint: str = Field(..., description='The visual cue used to determine hierarchy.')

    id: int | None = Field(None, description='Internal ID for mapping.')
    start_index: int | None = Field(None, description='Absolute character index in document.')
    verified: bool = Field(
        False, description='Whether this header was verified in the source text.'
    )


class PageIndexBlock(BaseModel):
    """A block of merged node content for embedding and retrieval."""

    id: str = Field(..., description='Content-hash ID for this block (md5 of content).')
    seq: int = Field(..., description='Sequential index for ordering within the document.')
    token_count: int = Field(..., description='Total tokens in this block.')
    start_index: int = Field(
        ..., description='Absolute start index of the first node in this block.'
    )
    end_index: int = Field(..., description='Absolute end index of the last node in this block.')
    titles_included: list[str] = Field(
        ..., description='List of section titles contained in this block.'
    )
    content: str = Field(..., description='The full, merged text content (including headers).')
    summary: BlockSummary | None = Field(
        None,
        description='Synthesized summary of this block based on its section summaries.',
    )


class TOCNode(BaseModel):
    """A node in the hierarchical table-of-contents tree."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    reasoning: str = Field(..., description='Explain why this node belongs at this specific level.')
    original_header_id: int = Field(..., description='The integer ID from the input list.')
    title: str = Field(..., description='The clean title.')
    level: int = Field(..., description='The hierarchical level.')

    children: list['TOCNode'] = Field(default_factory=list, description='Nested subsections.')

    start_index: int | None = None
    end_index: int | None = None
    content: str | None = Field(
        None, description='The immediate text content (excluding children).'
    )
    token_estimate: int | None = None

    summary: SectionSummary | None = Field(None, description='The 5W summary of the content.')

    block_id: str | None = Field(
        None,
        description="The hash ID of the Block this section's content was merged into.",
    )

    @property
    def content_hash(self) -> str | None:
        if self.content is None:
            return None
        return content_hash_md5(self.content)

    def _assign_content_hash_ids(self) -> None:
        """Replace UUID ids with content-hash ids. Call after hydration when content is set."""
        if self.content is not None:
            self.id = content_hash_md5(self.content)
        for child in self.children:
            child._assign_content_hash_ids()

    def tree_without_text(self, *, min_node_tokens: int = 0) -> dict[str, Any]:
        """Return a thin-tree dict with ``content`` and ``reasoning`` stripped at every level.

        Args:
            min_node_tokens: Drop nodes whose ``token_estimate`` is at or below this
                threshold.  Children of dropped nodes are promoted to the parent level.
        """
        data = self.model_dump(
            exclude={'content', 'reasoning'},
            mode='json',
        )
        # model_dump exclude only applies at the top level; recurse into children.
        children: list[dict[str, Any]] = []
        for child in self.children:
            if min_node_tokens > 0 and (child.token_estimate or 0) <= min_node_tokens:
                # Skip this node but promote its children
                for grandchild in child.children:
                    children.append(grandchild.tree_without_text(min_node_tokens=min_node_tokens))
            else:
                children.append(child.tree_without_text(min_node_tokens=min_node_tokens))
        data['children'] = children
        return data


class StructureQuality(BaseModel):
    """Assessment of how well-structured a document is based on its headers."""

    is_well_structured: bool
    header_count: int
    has_hierarchy: bool
    coverage_ratio: float
    avg_section_tokens: float
    max_gap_chars: int
    reason: str


class PageIndexOutput(BaseModel):
    """Complete output of the PageIndex indexing process."""

    toc: list[TOCNode]
    blocks: list[PageIndexBlock]
    node_to_block_map: dict[str, str]
    coverage_ratio: float = Field(
        0.0, description='Fraction of document covered by detected sections.'
    )
    path_used: str = Field(
        'unknown', description="Which indexing path was used: 'regex_fast' or 'llm_scan'."
    )

    def json_tree(self) -> str:
        return json.dumps([n.tree_without_text() for n in self.toc], indent=2)

    def get_block(self, node_id: str) -> PageIndexBlock | None:
        block_id = self.node_to_block_map.get(node_id)
        if block_id is None:
            return None
        for block in self.blocks:
            if block.id == block_id:
                return block
        return None


class CausalRelation(SQLModel):
    """Causal relationship"""

    relationship_type: CausalRelationshipTypes = Field(
        ...,
        description='Type of causal relationship. Use:\n'
        '1. **causes**: A causes B.\n'
        '2. **caused_by**: B is caused by A.\n'
        '3. **enables**: A enables B to happen.\n'
        '4. **prevents**: A prevents B from happening.',
    )
    target_fact_index: int = Field(
        ..., description='Index of the target fact in the list of extracted facts of a document.'
    )
    strength: float = Field(
        ...,
        description='Strength of the causal relationship, a float between 0 and 1.',
        ge=0.0,
        le=1.0,
    )

    @field_serializer('relationship_type')
    def serialize_relationship_type(self, relationship_type: CausalRelationshipTypes) -> str:
        return relationship_type.value


class StableBlock(SQLModel):
    """A content-addressed paragraph block for incremental diffing.

    Blocks are the unit of identity: each gets a SHA-256 hash of its
    whitespace-normalized text. The hash is used for order-independent
    diffing across document versions.
    """

    text: str = Field(..., description='The paragraph text.')
    content_hash: str = Field(..., description='SHA-256 of whitespace-normalized text.')
    block_index: int = Field(..., description='Position in the source document.')


@dataclass
class ProtectedZone:
    """A region of text that should not be split internally during CDC chunking."""

    start: int
    end: int
    zone_type: Literal['code_fenced', 'code_indented', 'list', 'frontmatter']
    can_split: bool
    split_points: list[int]


class ChunkMetadata(SQLModel):
    """Metadata for a text chunk."""

    chunk_text: str = Field(..., description='The text content of the chunk')
    embedding: list[float] | None = Field(
        default=None, description='The vector embedding of the chunk text'
    )
    fact_count: int = Field(..., description='Number of facts in the chunk')
    content_index: int = Field(
        ..., description='Index of the document in the original content, i.e. the Dth document.'
    )
    chunk_index: int = Field(
        ...,
        description='Index of the chunk among all chunks coming from all documents, i.e. if there are D '
        'documents with N and M chunks, then the last chunk would have index N + M.',
    )
    content_hash: str = Field(
        default='',
        description='SHA-256 hash of whitespace-normalized text for incremental diffing.',
    )


class RetainContent(SQLModel):
    """Input context for content retention."""

    content: str = Field(..., description='The content to be retained.')
    vault_id: UUID = Field(
        default=GLOBAL_VAULT_ID, description='The target vault for this content.'
    )
    event_date: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(tz=dt.timezone.utc),
        description='The date of when the content was created or relevant.',
    )
    context: str | None = Field(default=None, description='Additional context for the content.')
    payload: dict = Field(default_factory=dict, description='Additional metadata for the content.')


EntityType = Literal[
    'Person',
    'Organization',
    'Location',
    'Concept',
    'Technology',
    'File',
    'Misc',
]


class Entity(SQLModel):
    """An entity extracted from text."""

    text: str = Field(..., description='The specific, named entity.')
    entity_type: EntityType | None = Field(
        default=None,
        description='Category of the entity. '
        'Person: people. Organization: companies, teams, groups. Location: places. '
        'Concept: abstract ideas, themes. Technology: languages, frameworks, tools, databases. '
        'File: files, documents, paths. Misc: anything else.',
    )


class EntityLink(SQLModel):
    """Link between two memory units through a shared entity."""

    from_unit_id: UUID = Field(..., description='The UUID of the source memory unit.')
    to_unit_id: UUID = Field(..., description='The UUID of the target memory unit.')
    entity_id: UUID = Field(..., description='The UUID of the shared entity.')
    link_type: Literal['entity'] = Field(
        default='entity', description='The type of link, always "entity" for entity links.'
    )
    weight: float = Field(
        default=1.0,
        description='The strength of the link, a float between 0 and 1.',
        ge=0.0,
        le=1.0,
    )


class BaseFact(SQLModel):
    """Base class for facts."""

    causal_relations: list[CausalRelation] = Field(
        default_factory=list,
        description="Causal links to PREVIOUS facts only. target_index MUST be less than this fact's position. "
        'Example: fact #3 can only reference facts 0, 1, or 2. Max 2 relations per fact.',
    )
    entities: list[Entity] = Field(
        default_factory=list,
        description='Named entities, objects, AND abstract concepts from the fact. '
        'MANDATORY: ALL person names from "who" MUST appear here as Entity(text=name, entity_type="Person"). '
        'MANDATORY: ALL location names from "where" MUST appear here as Entity(text=place, entity_type="Location"). '
        'Also include: organizations, significant objects, AND abstract concepts/themes. '
        'Extract anything that could help link related facts together.',
    )
    fact_type: FactTypes = Field(
        ...,
        description='The type/category of the extracted fact. Use:\n'
        '1. **world**: Factual information about the world, definitions, system states, static knowledge, or outcomes of actions. Describes *what things are*, *how they function*, or *what is true* (e.g., "The system uses Python", "Project X is completed"). '
        'Classify as WORLD even if described with past-tense verbs like "established" or "implemented" if the core value is the resulting state.\n'
        '2. **event**: Specific episodic events, narrative occurrences, or actions that happened at a specific time. Describes *what happened* (narrative) to a person, system, or organization (e.g., "The server crashed", "We deployed v2", "We had a meeting"). '
        'Do NOT include facts that purely define a state or property, even if they have a start date.',
    )

    @field_serializer('fact_type')
    def serialize_fact_type(self, fact_type: FactTypes) -> str:
        return fact_type.value


class RawFact(BaseFact):
    """A single fact extracted from text by an LLM."""

    what: str = Field(
        ...,
        description='WHAT happened - COMPLETE, DETAILED description with ALL specifics. '
        'NEVER summarize or omit details. Include: exact actions, objects, quantities, specifics. '
        'BE VERBOSE - capture every detail that was mentioned. '
        "Example: 'Emily got married to Sarah at a rooftop garden ceremony with 50 guests attending and a live jazz band playing' "
        "NOT: 'A wedding happened' or 'Emily got married'",
    )
    when: str | None = Field(
        default=None,
        description='WHEN it happened - ALWAYS include temporal information if mentioned. '
        'Include: specific dates, times, durations, relative time references. '
        "Examples: 'on June 15th, 2024 at 3pm', 'last weekend', 'for the past 3 years', 'every morning at 6am'. "
        "Write 'N/A' ONLY if absolutely no temporal context exists. Prefer converting to absolute dates when possible.",
    )
    where: str | None = Field(
        default=None,
        description='WHERE it happened or is about - SPECIFIC locations, places, areas, regions if applicable. '
        'Include: cities, neighborhoods, venues, buildings, countries, specific addresses when mentioned. '
        "Examples: 'downtown San Francisco at a rooftop garden venue', 'at the user's home in Brooklyn', 'online via Zoom', 'Paris, France'. "
        "Write 'N/A' ONLY if absolutely no location context exists or if the fact is completely location-agnostic.",
    )
    who: str | None = Field(
        default=None,
        description='WHO is involved - ALL people/entities with FULL context and relationships. '
        'Include: names, roles, relationships to user, background details. '
        "Resolve coreferences (if 'my roommate' is later named 'Emily', write 'Emily, the user's college roommate'). "
        'BE DETAILED about relationships and roles. '
        "Example: 'Emily (user's college roommate from Stanford, now works at Google), Sarah (Emily's partner of 5 years, software engineer)' "
        "NOT: 'my friend' or 'Emily and Sarah'",
    )
    why: str | None = Field(
        default=None,
        description='WHY it matters - ALL emotional, contextual, and motivational details. '
        'Include EVERYTHING: feelings, preferences, motivations, context, background, significance. '
        'BE VERBOSE - capture all the nuance and meaning. '
        'FOR ASSISTANT FACTS: MUST include what the user asked/requested that led to this interaction! '
        "DO NOT refer to other facts by index/number (e.g. 'because of Fact 1'). Explain the reasoning semantically. "
        "Example (world): 'The user felt thrilled and inspired, has always dreamed of an outdoor ceremony, mentioned "
        "wanting a similar garden venue, was particularly moved by the intimate atmosphere and personal vows' "
        "Example (assistant): 'User asked how to fix slow API performance with 1000+ concurrent users, expected 70-80% "
        "reduction in database load' NOT: 'User liked it' or 'To help user'",
    )
    fact_kind: FactKindTypes = Field(
        default=FactKindTypes.CONVERSATION,
        description="'dated' = specific datable occurrence (set occurred dates), "
        "'conversation' = general info (no occurred dates)",
    )
    occurred_start: str | None = Field(
        default=None,
        description='Exact date in ISO 8601 format (YYYY-MM-DD). ONLY use if a specific date is present. Otherwise None. '
        "WHEN the event happened (ISO timestamp). Only for fact_kind='dated'. Leave null for conversations. If a `date "
        "is mentioned in 'when', you **MUST** convert it here.",
    )
    occurred_end: str | None = Field(
        default=None,
        description='Exact date in ISO 8601 format (YYYY-MM-DD). ONLY use if a specific date is present. Otherwise None. '
        'WHEN the event ended (ISO timestamp). Only for dated events with duration. Leave null for conversations. '
        "If an **end date** is mentioned in 'when', you **MUST** convert it here.",
    )

    @field_serializer('fact_kind')
    def serialize_fact_kind(self, fact_kind: FactKindTypes) -> str:
        return fact_kind.value

    @field_validator('fact_type', mode='before')
    @classmethod
    def normalize_type(cls, v: str) -> str:
        # Handle LLM tendencies to output 'assistant' or old 'experience' value
        if v in ('assistant', 'experience'):
            return 'event'
        return v

    @field_validator('occurred_start', 'occurred_end')
    @classmethod
    def validate_date_format(cls, v: str | None) -> str | None:
        if v is None:
            return None

        if v.upper() in ['N/A', 'NONE', 'UNKNOWN']:
            return None

        if not re.match(DATE_REGEX, v):
            raise ValueError(f"Date '{v}' must be in YYYY-MM-DD format.")

        return v

    @property
    def formatted_text(self) -> str:
        """
        Reconstructs the combined text format used by the legacy application:
        'What | When | Involving: Who | Why'
        """
        parts = [self.what]
        if self.when and str(self.when).upper() != 'N/A':
            parts.append(f'When: {self.when}')
        if self.who and str(self.who).upper() != 'N/A':
            parts.append(f'Involving: {self.who}')
        if self.why and str(self.why).upper() != 'N/A':
            parts.append(self.why)
        return ' | '.join(parts)


class ExtractedOutput(SQLModel):
    """Output of the extraction process for a single content item."""

    extracted_facts: list[RawFact] = Field(
        ..., description='List of extracted facts from the content.'
    )


class ExtractedFact(BaseFact):
    """Final, extracted fact from content."""

    fact_text: str = Field(..., description='The text of the extracted fact.')
    vault_id: UUID = Field(
        default=GLOBAL_VAULT_ID, description='The vault ID associated with this fact.'
    )
    content_index: int = Field(
        ..., description='Index of the document in the original content, i.e. the Dth document.'
    )
    chunk_index: int = Field(
        ...,
        description='Index of the Nth chunk from the Dth document from which the fact was extracted.',
    )
    context: str | None = Field(
        default=None, description='Additional context for the extracted fact.'
    )
    mentioned_at: dt.datetime = Field(
        ...,
        description='The timestamp of when the the document containign the fact was added. Same as event_date in RetainContent.',
    )
    payload: dict = Field(
        default_factory=dict, description='Additional metadata for the extracted fact.'
    )
    occurred_start: None | dt.datetime = Field(
        default=None,
        description='The start time of the event described by the fact, if applicable.',
    )
    occurred_end: None | dt.datetime = Field(
        default=None, description='The end time of the event described by the fact, if applicable.'
    )
    who: str | None = Field(default=None, description='People/entities involved in the fact.')
    where: str | None = Field(
        default=None, description='Location information associated with the fact.'
    )


class ProcessedFact(SQLModel):
    """A fact that has been processed and is ready for storage."""

    fact_text: str = Field(..., description='The textual content of the fact.')
    fact_type: FactTypes = Field(..., description='The category/type of the fact.')
    embedding: list[float] = Field(
        ..., description='The vector embedding representation of the fact.'
    )

    occurred_start: dt.datetime | None = Field(
        default=None,
        description='The start timestamp of the event, if applicable.',
    )
    occurred_end: dt.datetime | None = Field(
        default=None,
        description='The end timestamp of the event, if applicable.',
    )
    mentioned_at: dt.datetime = Field(
        ..., description='The timestamp when this fact was mentioned/recorded.'
    )

    context: str = Field(default='', description='Surrounding context where the fact was found.')
    payload: dict[str, Any] = Field(
        default_factory=dict, description='Additional key-value metadata.'
    )

    who: str | None = Field(default=None, description='People/entities involved in the fact.')
    where: str | None = Field(
        default=None, description='Location information associated with the fact.'
    )

    entities: list[Entity] = Field(
        default_factory=list, description='List of entities referenced in the fact.'
    )
    causal_relations: list[CausalRelation] = Field(
        default_factory=list, description='Causal relationships to other facts.'
    )

    chunk_id: str | None = Field(default=None, description='ID of the text chunk source.')
    note_id: str | None = Field(default=None, description='ID of the source note.')
    vault_id: UUID = Field(
        default=GLOBAL_VAULT_ID, description='The vault ID associated with this fact.'
    )

    unit_id: UUID | None = Field(
        default=None,
        description='The unique identifier assigned after storage (if persisted).',
    )
    content_index: int = Field(default=0, description='Index indicating the order of the content.')
    tags: list[str] = Field(
        default_factory=list, description='Tags for categorization or filtering.'
    )

    @field_validator('occurred_start', 'occurred_end', 'mentioned_at')
    @classmethod
    def ensure_timezone(cls, v: dt.datetime | None) -> dt.datetime | None:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=dt.timezone.utc)
        return v

    @property
    def is_duplicate(self) -> bool:
        """Check if the fact is marked as a duplicate (has no unit_id)."""
        return self.unit_id is None

    @classmethod
    def from_extracted_fact(
        cls,
        extracted_fact: ExtractedFact,
        embedding: list[float],
        chunk_id: str | None = None,
    ) -> 'ProcessedFact':
        """
        Create a ProcessedFact from an ExtractedFact.

        Args:
            extracted_fact: The source ExtractedFact.
            embedding: The vector embedding for the fact.
            chunk_id: Optional ID of the source chunk.

        Returns:
            A new ProcessedFact instance.
        """
        return cls(
            fact_text=extracted_fact.fact_text,
            fact_type=extracted_fact.fact_type,
            embedding=embedding,
            occurred_start=extracted_fact.occurred_start,
            occurred_end=extracted_fact.occurred_end,
            mentioned_at=extracted_fact.mentioned_at,
            context=extracted_fact.context or '',
            payload={k: str(v) for k, v in extracted_fact.payload.items()},
            who=extracted_fact.who,
            where=extracted_fact.where,
            entities=extracted_fact.entities,
            causal_relations=extracted_fact.causal_relations,
            chunk_id=chunk_id,
            content_index=extracted_fact.content_index,
            vault_id=extracted_fact.vault_id,  # Explicitly pass vault_id
        )
