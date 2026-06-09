"""V7 Experiential Plane — Pydantic DTOs and shared enums.

The experiential plane stores procedural memory as three entity kinds:

* **case** — a record of a specific experience (what happened, with what
  trigger). Embedding lives on ``trigger_embedding``.
* **procedure** — a synthesised how-to recipe (verb + optional context).
  Embedding lives on ``body_embedding``.
* **strategy** — an opinionated play-book that picks a procedure for a
  (verb, context) tuple. Embedding lives on ``body_embedding``.

The DTOs in this module are the public envelope used by the API facade, the
HTTP routes, the MCP tools, the CLI, and the Hermes plugin. They deliberately
do not import from ``memex_core`` — the SQLModel enums are mirrored as
``Literal`` types so the same string contract is enforced on both sides of
the boundary. Mismatches are caught at the ORM layer by the DB CHECK
constraints (see migration 061).

The brief and the V7 design doc are the source of truth for the field set.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# --- shared enums (mirror memex_core.memory.sql_models) -------------------

# These enums are the SSOT for the string contract used by the ORM CHECK
# constraints in migration 061. If you change a value, update:
#   1. memex_core/memory/sql_models.py (the SQLModel enums)
#   2. migration 061 (the CHECK constraint)
#   3. the tests in test_int_alembic_061.py (the CHECK smoke tests)
#   4. this module (the Literal/StrEnum definitions)


class ExperientialKind(str, Enum):
    """The three entity kinds in the experiential plane."""

    CASE = 'case'
    PROCEDURE = 'procedure'
    STRATEGY = 'strategy'


class ExperientialStatus(str, Enum):
    """Lifecycle state of an experiential entry.

    * draft — created but not yet promoted; not visible to search/briefing.
    * published — visible to agents via search/briefing.
    * deprecated — superseded by another entry; kept for lineage.
    """

    DRAFT = 'draft'
    PUBLISHED = 'published'
    DEPRECATED = 'deprecated'


class ExperientialOrigin(str, Enum):
    """How an experiential entry came to exist."""

    SEED = 'seed'  # boot-time system seed (migration 063)
    KV_BACKFILL = 'kv_backfill'  # promoted from a legacy <scope>:procedure:* KV row
    DERIVED = 'derived'  # LLM-derived from cases (derivation queue)
    MANUAL = 'manual'  # agent-written
    IMPORT = 'import'  # bulk import


class ExperientialSourceRole(str, Enum):
    """Role an experiential_source row plays in a relationship."""

    PROVENANCE = 'provenance'  # case that gave rise to a procedure
    EVIDENCE = 'evidence'  # supporting fact for a procedure/strategy
    CONTRADICTION = 'contradiction'  # case that argues against a procedure


class DerivationQueueStatus(str, Enum):
    """State of a row in the async derivation queue."""

    PENDING = 'pending'
    IN_PROGRESS = 'in_progress'
    COMPLETED = 'completed'
    FAILED = 'failed'


# Validated string aliases — used in DTOs so the same contract is enforced
# whether a caller passes a Python enum or a raw string.
KindLiteral = Literal['case', 'procedure', 'strategy']
StatusLiteral = Literal['draft', 'published', 'deprecated']
OriginLiteral = Literal['seed', 'kv_backfill', 'derived', 'manual', 'import']
SourceRoleLiteral = Literal['provenance', 'evidence', 'contradiction']
DerivationStatusLiteral = Literal['pending', 'in_progress', 'completed', 'failed']

# Constraint alias for short, identifier-style scope/verb/context strings.
# The schema does not enforce a length cap, but agents query on these so
# overly-long values are a UX hazard. Mirrors the existing NoteKey convention.
ShortLabel = Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=True)]


# --- core entry DTOs ------------------------------------------------------


class ExperientialSourceDTO(BaseModel):
    """A single source edge attached to an experiential entry."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    entry_id: UUID
    source_entry_id: UUID | None = None
    source_note_id: UUID | None = None
    source_memory_unit_id: UUID | None = None
    role: SourceRoleLiteral
    weight: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description='Edge weight used in RRF aggregation. Bounded [0.0, 10.0].',
    )
    created_at: dt.datetime


class ExperientialPinDTO(BaseModel):
    """A context-binding pin that anchors an entry into a pin chain."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    context_key: ShortLabel
    entry_id: UUID
    position: int = Field(ge=0)
    pinned_by: str | None = None
    created_at: dt.datetime


class ExperientialEntryDTO(BaseModel):
    """Public-facing representation of an experiential entry.

    Mirrors the SQLModel column set with embedding vectors omitted (those
    are not meaningful to call-sites; they live in the search path).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vault_id: UUID
    kind: KindLiteral
    scope: ShortLabel
    verb: str | None = None
    context: str | None = None
    title: ShortLabel
    summary: str
    body: str = ''
    trigger: str | None = None
    tags: list[str] = Field(default_factory=list)
    extra_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description='Arbitrary metadata — confidence, run_count, last_verified_at, etc.',
    )
    status: StatusLiteral = 'draft'
    origin: OriginLiteral = 'manual'
    supersedes_id: UUID | None = None
    superseded_by_id: UUID | None = None
    published_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    # Lineage / pointer fields surfaced for agent readability.
    sources: list[ExperientialSourceDTO] = Field(default_factory=list)
    pins: list[ExperientialPinDTO] = Field(default_factory=list)


# --- mutation DTOs --------------------------------------------------------


class ExperientialEntryCreate(BaseModel):
    """Create a new experiential entry.

    The repository's identity-anchor rule applies: a (kind, scope, verb,
    context) tuple must be unique for procedures and strategies. Cases
    ignore the identity anchor (verb and context are NULL).
    """

    model_config = ConfigDict(extra='forbid')

    vault_id: UUID
    kind: KindLiteral
    scope: ShortLabel
    verb: str | None = None
    context: str | None = None
    title: ShortLabel
    summary: str
    body: str = ''
    trigger: str | None = None
    tags: list[str] = Field(default_factory=list)
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    status: StatusLiteral = 'draft'
    origin: OriginLiteral = 'manual'
    supersedes_id: UUID | None = None


class ExperientialEntryUpdate(BaseModel):
    """Mutate an existing entry in place.

    All fields are optional; only set fields are updated. The repository
    appends a new ``experiential_entry_versions`` row on every successful
    update — see V7 design §3.2.
    """

    model_config = ConfigDict(extra='forbid')

    title: ShortLabel | None = None
    summary: str | None = None
    body: str | None = None
    trigger: str | None = None
    tags: list[str] | None = None
    extra_metadata: dict[str, Any] | None = None
    status: StatusLiteral | None = None
    supersedes_id: UUID | None = None
    edit_reason: str | None = None
    edited_by: str | None = None


class ExperientialSourceCreate(BaseModel):
    """Attach a source edge to an entry."""

    model_config = ConfigDict(extra='forbid')

    source_entry_id: UUID | None = None
    source_note_id: UUID | None = None
    source_memory_unit_id: UUID | None = None
    role: SourceRoleLiteral = 'evidence'
    weight: float = Field(default=1.0, ge=0.0, le=10.0)


class ExperientialPinCreate(BaseModel):
    """Pin an entry into a context-binding chain."""

    model_config = ConfigDict(extra='forbid')

    context_key: ShortLabel
    entry_id: UUID
    position: int = Field(ge=0)
    pinned_by: str | None = None


# --- derivation queue DTOs ------------------------------------------------


class ExperientialDerivationQueueDTO(BaseModel):
    """A pending or in-progress derivation task."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vault_id: UUID
    source_entry_ids: list[UUID] = Field(default_factory=list)
    target_kind: Literal['procedure', 'strategy']
    target_scope: ShortLabel
    target_verb: str | None = None
    target_context: str | None = None
    status: DerivationStatusLiteral = 'pending'
    attempt_count: int = 0
    last_error: str | None = None
    result_entry_id: UUID | None = None
    claimed_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    created_at: dt.datetime


class ExperientialDerivationQueueClaim(BaseModel):
    """What a worker needs to start working on a queue row."""

    model_config = ConfigDict(from_attributes=True)

    queue_id: UUID
    vault_id: UUID
    source_entry_ids: list[UUID]
    target_kind: Literal['procedure', 'strategy']
    target_scope: ShortLabel
    target_verb: str | None = None
    target_context: str | None = None


# --- search DTOs ----------------------------------------------------------


class ExperientialSearchRequest(BaseModel):
    """Hybrid BM25 + vector search across the experiential plane.

    At least one of ``query`` and ``scope`` must be set. The kind filter
    scopes the result to a single entity class; status defaults to
    ``published`` to match the briefing semantics.
    """

    model_config = ConfigDict(extra='forbid')

    query: str | None = None
    scope: ShortLabel | None = None
    kind: KindLiteral | None = None
    status: StatusLiteral = 'published'
    limit: int = Field(default=10, ge=1, le=100)
    bm25_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    include_pin_chain: bool = Field(
        default=False,
        description='When true, also include entries pinned at the supplied '
        '`pin_contexts` list (in addition to the textual/vector hits).',
    )
    pin_contexts: list[ShortLabel] = Field(default_factory=list)


class ExperientialSearchHit(BaseModel):
    """A single hit from the experiential search service."""

    model_config = ConfigDict(from_attributes=True)

    entry: ExperientialEntryDTO
    score: float = Field(
        description='Final RRF-aggregated score. Higher is better.',
    )
    bm25_rank: int | None = None
    vector_rank: int | None = None
    matched_via: Literal['bm25', 'vector', 'rrf', 'pin'] = 'rrf'
    pin_position: int | None = Field(
        default=None,
        description='Position within the pin chain when matched_via == "pin".',
    )


class ExperientialSearchResponse(BaseModel):
    """Result envelope for an experiential-plane search."""

    hits: list[ExperientialSearchHit] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    took_ms: float = 0.0


class ExperientialBriefingCard(BaseModel):
    """A single card in the session briefing's experiential slot.

    The briefing renders one card per entry with: title, kind badge, a
    truncated summary, and the matched trigger / scope. The agent decides
    whether to expand any card on demand.
    """

    model_config = ConfigDict(from_attributes=True)

    entry: ExperientialEntryDTO
    pin_position: int
    context_key: ShortLabel


class ExperientialBriefingCards(BaseModel):
    """Envelope for the briefing surface — the cards pre-sorted by pin order."""

    cards: list[ExperientialBriefingCard] = Field(default_factory=list)
    context_keys: list[ShortLabel] = Field(
        default_factory=list,
        description='The pin contexts that contributed entries to this briefing.',
    )
    total_pinned: int = 0


__all__ = [
    'DerivationQueueStatus',
    'DerivationStatusLiteral',
    'ExperientialBriefingCard',
    'ExperientialBriefingCards',
    'ExperientialDerivationQueueClaim',
    'ExperientialDerivationQueueDTO',
    'ExperientialEntryCreate',
    'ExperientialEntryDTO',
    'ExperientialEntryUpdate',
    'ExperientialKind',
    'ExperientialOrigin',
    'ExperientialPinCreate',
    'ExperientialPinDTO',
    'ExperientialSearchHit',
    'ExperientialSearchRequest',
    'ExperientialSearchResponse',
    'ExperientialSourceCreate',
    'ExperientialSourceDTO',
    'ExperientialSourceRole',
    'ExperientialStatus',
    'KindLiteral',
    'OriginLiteral',
    'ShortLabel',
    'SourceRoleLiteral',
    'StatusLiteral',
]
