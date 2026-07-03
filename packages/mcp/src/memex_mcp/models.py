"""Memex lifecycle model and response models"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, List, Literal
from uuid import UUID

import ast

from pydantic import BaseModel, Field, PrivateAttr, field_validator

from memex_common.asset_cache import SessionAssetCache
from memex_common.client import RemoteMemexAPI
from memex_common.config import MemexConfig
from memex_common.schemas import SectionAssetDTO, TOCNodeDTO


class Staleness(str, Enum):
    """Staleness indicator for memory search results."""

    FRESH = 'fresh'
    AGING = 'aging'
    STALE = 'stale'
    CONTESTED = 'contested'


class AppContext(BaseModel):
    """Application context for the Memex MCP server."""

    config: MemexConfig = Field(..., description='The Memex configuration settings.')
    _api: RemoteMemexAPI = PrivateAttr()
    _asset_cache: SessionAssetCache | None = PrivateAttr(default=None)

    model_config = {'arbitrary_types_allowed': True}


class NERModel(BaseModel):
    """A named entity recognized in a note."""

    text: str = Field(..., description='The recognized entity text.')
    type: str = Field(..., description='The type of entity (e.g., PERSON, ORG).')


class FactItem(BaseModel):
    """A fact item in a narrative."""

    statement: str
    note_uuid: str


class EventItem(BaseModel):
    """An event item in a narrative."""

    statement: str
    note_uuid: str


class Narrative(BaseModel):
    """The model-generated narrative extracted from the note."""

    fact: List[FactItem] = Field(default_factory=list)
    event: List[EventItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# MCP tool return models — structured output for FastMCP v3
# ---------------------------------------------------------------------------


# ── Memory unit hierarchy ──


class McpSupersession(BaseModel):
    unit_id: UUID
    unit_text: str
    relation: str  # 'contradicts' | 'weakens'
    note_title: str | None = None


class McpCitation(BaseModel):
    """Evidence unit supporting an observation."""

    unit_id: UUID
    text: str
    date: str | None = None


class McpMemoryLink(BaseModel):
    """A link between memory units."""

    unit_id: UUID
    note_id: UUID | None = None
    note_title: str | None = None
    relation: str
    weight: float = 1.0
    time: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class McpRelatedNote(BaseModel):
    """A note related via shared entities."""

    note_id: UUID
    title: str | None = None
    shared_entities: list[str] = Field(default_factory=list)
    strength: float = 0.0


class McpMemoryUnitBase(BaseModel):
    """Shared fields across all memory unit types."""

    id: UUID
    text: str
    fact_type: str
    score: float | None = None
    confidence: float = 1.0
    note_id: UUID | None = None
    note_title: str | None = None
    node_ids: list[str] = []
    tags: list[str] = []
    status: str = 'active'
    superseded_by: list[McpSupersession] = []
    links: list[McpMemoryLink] = Field(default_factory=list)
    staleness: Staleness | None = None
    previously_returned: bool = False
    # Virtual units are synthesized from MentalModel observations — their `id`
    # is a deterministic placeholder, not a DB row. When `virtual=True`, agents
    # should not point-lookup `id`; resolve via `evidence_ids` instead.
    virtual: bool = False
    mental_model_id: UUID | None = None
    evidence_ids: list[UUID] = Field(default_factory=list)
    success_co_count: int = 0
    failure_co_count: int = 0
    is_deprioritized: bool = False
    intent_class: str = 'durable'
    risk_class: str = 'none'
    exploration: bool = False
    created_at: datetime | None = None
    event_date: datetime | None = None

    @field_validator('tags', mode='before')
    @classmethod
    def _coerce_tags(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                parsed = ast.literal_eval(v)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed]
            except (ValueError, SyntaxError):
                return [v] if v else []
        return v if isinstance(v, list) else []


class McpFact(McpMemoryUnitBase):
    """Timeless world fact."""

    fact_type: Literal['world'] = 'world'


class McpEvent(McpMemoryUnitBase):
    """Something that happened at a specific time."""

    fact_type: Literal['event'] = 'event'
    occurred_start: datetime | None = None
    occurred_end: datetime | None = None


class McpObservation(McpMemoryUnitBase):
    """Behavioral pattern or preference observed over time."""

    fact_type: Literal['observation'] = 'observation'
    mentioned_at: datetime | None = None
    citations: list[McpCitation] = []


McpMemoryUnit = Annotated[
    McpFact | McpEvent | McpObservation,
    Field(discriminator='fact_type'),
]


# ── Entity models ──


class McpEntity(BaseModel):
    id: UUID
    name: str
    type: str | None = None
    mention_count: int = 0
    description: str | None = None


class McpEntityMention(BaseModel):
    unit_id: UUID
    text: str
    fact_type: str
    note_id: UUID | None = None
    note_title: str | None = None


class McpCooccurrence(BaseModel):
    entity_id: UUID
    entity_name: str
    entity_type: str | None = None
    count: int


# ── Note search & find ──


class McpNoteSearchResult(BaseModel):
    note_id: UUID
    title: str
    score: float
    vault_name: str | None = None
    status: str | None = None
    description: str | None = None
    tags: list[str] = []
    source_uri: str | None = None
    has_assets: bool = False
    created_at: datetime | None = None
    publish_date: datetime | None = None
    related_notes: list[McpRelatedNote] = Field(default_factory=list)
    links: list[McpMemoryLink] = Field(default_factory=list)
    previously_returned: bool = False

    @field_validator('tags', mode='before')
    @classmethod
    def _coerce_tags(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                parsed = ast.literal_eval(v)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed]
            except (ValueError, SyntaxError):
                return [v] if v else []
        return v if isinstance(v, list) else []


class McpFindResult(BaseModel):
    note_id: UUID
    title: str
    score: float
    status: str
    publish_date: date | None = None


# ── Page index & nodes ──


class McpPageMetadata(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] = []
    publish_date: str | None = None
    source_uri: str | None = None
    has_assets: bool = False
    vault_name: str | None = None
    total_tokens: int | None = None


class McpPageIndex(BaseModel):
    note_id: UUID
    metadata: McpPageMetadata
    toc: list[TOCNodeDTO]
    total_tokens: int | None = None
    related_notes: list[McpRelatedNote] = Field(default_factory=list)


class McpNoteMetadata(BaseModel):
    note_id: UUID
    title: str
    total_tokens: int | None = None
    vault_name: str | None = None
    tags: list[str] = []
    has_assets: bool = False
    created_at: datetime | None = None
    publish_date: datetime | None = None


class McpNode(BaseModel):
    id: UUID
    note_id: UUID
    title: str
    text: str | None = None
    level: int
    block_id: UUID | None = None
    assets: list[SectionAssetDTO] = Field(default_factory=list)


# ── Note listing ──


class McpNoteSummary(BaseModel):
    """Block-level summary for note listings."""

    topic: str
    key_points: list[str] = Field(default_factory=list)


class McpNote(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    publish_date: datetime | None = None
    vault_id: UUID | None = None
    template: str | None = None
    summaries: list[McpNoteSummary] = Field(default_factory=list)


class McpNoteContent(BaseModel):
    """Full note content — only for small notes (< 500 tokens)."""

    id: UUID
    title: str
    description: str | None = None
    vault_id: UUID
    created_at: datetime
    content: str | None = None


# ── Vault ──


class McpVault(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    kind: str = 'content'
    is_active: bool = False
    note_count: int = 0
    last_note_added_at: datetime | None = None
    access: list[str] | None = None


# ── Assets ──


class McpAsset(BaseModel):
    filename: str
    path: str
    mime_type: str | None = None


class McpAddAssetsResult(BaseModel):
    note_id: str
    added_assets: list[McpAsset]
    skipped: list[str]
    asset_count: int


class McpDeleteAssetsResult(BaseModel):
    note_id: str
    deleted: list[str]
    not_found: list[str]
    asset_count: int


# ── KV store ──


class McpKVEntry(BaseModel):
    key: str
    value: str
    scope: str
    updated_at: datetime
    expires_at: datetime | None = None


class McpKVPutResult(BaseModel):
    key: str
    value: str
    scope: str
    expires_at: datetime | None = None


def _scope_from_key(key: str) -> str:
    """Derive scope from the namespace prefix of a key.

    Examples:
        'global:foo' -> 'global'
        'user:work:employer' -> 'user'
        'project:github.com/user/repo:vault' -> 'project:github.com/user/repo'
    """
    if key.startswith('project:'):
        # project:<project-id>:<setting> -> scope is project:<project-id>
        rest = key[len('project:') :]
        colon_idx = rest.rfind(':')
        if colon_idx > 0:
            return f'project:{rest[:colon_idx]}'
        return 'project'
    # ':leading' (empty prefix before the colon) is treated as unknown.
    result = key.split(':', 1)[0] if ':' in key else ''
    return result or 'unknown'


# ── Add note ──


class McpOverlap(BaseModel):
    note_id: UUID
    title: str
    similarity_pct: int


class McpAddNoteResult(BaseModel):
    note_id: UUID
    status: str
    job_id: str | None = None
    overlapping_notes: list[McpOverlap] = []


class McpAppendNoteResult(BaseModel):
    """Outcome of memex_append_note. Compact by design — full DTO via memex_read_note."""

    note_id: UUID
    append_id: UUID
    status: str  # 'success' or 'replayed'
    delta_bytes: int
    new_unit_count: int


# ── Lineage ──


class McpLineageNode(BaseModel):
    """Recursive lineage node tracing provenance between entities."""

    entity_type: str
    entity: dict[str, Any]
    derived_from: list['McpLineageNode'] = []


McpLineageNode.model_rebuild()


# ── Survey ──


class McpSurveyFact(BaseModel):
    """A single fact in a survey topic."""

    id: UUID
    text: str
    fact_type: str
    score: float | None = None


class McpSurveyTopic(BaseModel):
    """A group of facts from a single source note."""

    note_id: UUID
    title: str | None = None
    fact_count: int
    facts: list[McpSurveyFact] = []


class McpSurveyResult(BaseModel):
    """Result from a broad topic survey."""

    query: str
    sub_queries: list[str]
    topics: list[McpSurveyTopic] = []
    total_notes: int = 0
    total_facts: int = 0
    truncated: bool = False


# ── Procedural plane  ──


class McpProceduralSource(BaseModel):
    """Source pointer on an procedural entry.

    Exactly one pointer is set (DB CHECK): ``entry_id`` for a strategy's
    backing procedure, ``note_id`` for a case, ``memory_unit_id`` for a
    fact. Mirrors the DTO's ``source_entry_id`` / ``source_note_id`` /
    ``source_memory_unit_id``.
    """

    model_config = {'extra': 'forbid'}

    entry_id: UUID | None = None
    note_id: UUID | None = None
    memory_unit_id: UUID | None = None
    role: str


class McpProceduralPin(BaseModel):
    """Pin linking an entry to a context key in the briefing pin chain."""

    model_config = {'extra': 'forbid'}

    context_key: str
    position: int


class McpProceduralEntry(BaseModel):
    """Public-facing procedural entry. Embedding vectors are omitted — they
    are not meaningful at the LLM-tool boundary; they live in the search path.

    ``vault_id`` is deliberately omitted: procedures are presented to agents as
    vault-agnostic knowledge, and the entries physically live in a hidden
    ``procedural`` system vault. Echoing that backing vault id out the tool
    boundary leaks storage plumbing the agent must not reason about (it would
    let an agent infer the system vault and the cross-tenant sharing model).
    The id stays available server-side for the vault-scope guardrail.
    """

    model_config = {'extra': 'forbid'}

    id: UUID
    kind: Literal['procedure', 'strategy']
    scope: str
    verb: str | None = None
    context: str | None = None
    title: str
    summary: str
    body: str = ''
    trigger: str | None = None
    tags: list[str] = Field(default_factory=list)
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    status: Literal['draft', 'published', 'deprecated'] = 'draft'
    # Must mirror memex_common.procedural_schemas.OriginLiteral exactly — the
    # DTO can emit any of these five and _dto_to_mcp_entry copies origin through.
    origin: Literal['seed', 'derived', 'authored', 'manual', 'import'] = 'manual'
    supersedes_id: UUID | None = None
    superseded_by_id: UUID | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    sources: list[McpProceduralSource] = Field(default_factory=list)
    pins: list[McpProceduralPin] = Field(default_factory=list)


class McpProceduralSearchHit(BaseModel):
    """One hit from the procedural hybrid search."""

    model_config = {'extra': 'forbid'}

    entry_id: UUID
    kind: Literal['procedure', 'strategy']
    score: float
    matched_via: Literal['bm25', 'vector', 'pin', 'rrf']
    title: str
    summary: str
    scope: str
    verb: str | None = None
    context: str | None = None
    trigger: str | None = None
    pin_position: int | None = None


class McpProceduralSearchResult(BaseModel):
    """Enveloped hits from memex_procedural_search."""

    model_config = {'extra': 'forbid'}

    hits: list[McpProceduralSearchHit] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    took_ms: float = 0.0


class McpCaseSubmitResult(BaseModel):
    """Result envelope for memex_case_submit.

    ``assignment_mode='escalated'`` means a contested assignment landed
    in the lint queue (``finding_id``) — resolve via the lint tools or
    leave it for human review (file-then-lint).

    ``assignment_mode='queued'`` means the case was filed in the background
    (``background=true``): ``job_id`` tracks the durable job and ``note_id`` /
    assignment fields are absent — the assignment resolves async and any
    escalation / new-procedure draft surfaces in the lint queue.

    ``vault_id`` is deliberately omitted: the backing ``procedural`` system
    vault is storage plumbing the agent must not see (mirrors the same
    redaction on McpProceduralEntry).
    """

    model_config = {'extra': 'forbid'}

    note_id: UUID | None = None
    job_id: UUID | None = None
    assignment_mode: Literal[
        'explicit', 'auto_assigned', 'new_procedure_draft', 'escalated', 'skipped', 'queued'
    ]
    entry_id: UUID | None = None
    finding_id: UUID | None = None
    separation: str | None = None
    reasoning: str | None = None
    scope: str | None = None
    scope_reasoning: str | None = None
