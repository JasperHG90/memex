"""Procedural Plane — Pydantic DTOs and shared enums.

The procedural plane stores procedural memory as exactly two entity kinds:

* **procedure** — a how-to recipe. Anchor ≡ (scope, verb, context), all
  required. Retrieval anchors on ``trigger`` (when_to_use).
* **strategy** — an opinionated play-book generalising over the procedures
  that share its (scope, verb). Anchor ≡ (scope, verb, NULL) — context is
  FORBIDDEN. Retrieval anchors on ``trigger`` (when_to_apply).

**Cases are NOT on this plane.** A case is a note (``notes.role='case'``)
filed into the hidden ``procedural`` system vault via case_submit
(design §5.1 / §18.3 / §18.9.0); it feeds procedures/strategies as lineage
through ``procedural_sources``.

Scope grammar: ``global`` | ``project:<id>`` | ``app:<id>``. There is NO
``user`` scope — procedures and strategies are shared, auto-generated
knowledge; per-user briefing curation rides the pin chain's context keys
(JG decision 2026-06-10).

The DTOs in this module are the public envelope used by the API facade, the
HTTP routes, the MCP tools, the CLI, and the Hermes plugin. They deliberately
do not import from ``memex_core`` — the SQLModel enums are mirrored as
``Literal`` types so the same string contract is enforced on both sides of
the boundary. Mismatches are caught at the ORM layer by the DB CHECK
constraints (see migrations 061 + 064).

The design doc is the source of truth for the field set.
"""

from __future__ import annotations

import datetime as dt
import re
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# --- shared enums (mirror memex_core.memory.sql_models) -------------------

# These enums are the SSOT for the string contract used by the ORM CHECK
# constraints in migration 061. If you change a value, update:
#   1. memex_core/memory/sql_models.py (the SQLModel enums)
#   2. migration 061 (the CHECK constraint)
#   3. the tests in test_int_alembic_061.py (the CHECK smoke tests)
#   4. this module (the Literal/StrEnum definitions)


class ProceduralKind(str, Enum):
    """The two entity kinds in the procedural plane.

    Cases are notes (``role='case'``), not procedural entries.
    """

    PROCEDURE = 'procedure'
    STRATEGY = 'strategy'


class ProceduralStatus(str, Enum):
    """Lifecycle state of an procedural entry.

    * draft — created but not yet promoted; not visible to search/briefing.
    * published — visible to agents via search/briefing.
    * deprecated — superseded by another entry; kept for lineage.
    """

    DRAFT = 'draft'
    PUBLISHED = 'published'
    DEPRECATED = 'deprecated'


class ProceduralOrigin(str, Enum):
    """How an procedural entry came to exist."""

    SEED = 'seed'  # boot-time system seed
    DERIVED = 'derived'  # LLM-derived from cases (derivation queue)
    AUTHORED = 'authored'  # hand-edited by a human/agent — sticky (§18.6.4)
    MANUAL = 'manual'  # agent-written
    IMPORT = 'import'  # bulk import


class ProceduralSourceRole(str, Enum):
    """Role an procedural_source row plays in a relationship."""

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
KindLiteral = Literal['procedure', 'strategy']
StatusLiteral = Literal['draft', 'published', 'deprecated']
OriginLiteral = Literal['seed', 'derived', 'authored', 'manual', 'import']
SourceRoleLiteral = Literal['provenance', 'evidence', 'contradiction']
DerivationStatusLiteral = Literal['pending', 'in_progress', 'completed', 'failed']

# Constraint alias for short, identifier-style scope/verb/context strings.
# The schema does not enforce a length cap, but agents query on these so
# overly-long values are a UX hazard. Mirrors the existing NoteKey convention.
ShortLabel = Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=True)]

# Scope / pin-context grammar (design §18.9.0 + §19.8). One segment for
# global, two for project:<id> and app:<id>, three for the Hermes
# per-agent pin context app:hermes:<agent_identity>. NO `user` scope —
# see module docstring.
#
# The <id> charset includes `/` so a git-remote-style project id like
# `github.com/owner/repo` is a valid scope. That form is the canonical
# project id everywhere else (KV `project:<id>:…` namespaces and the
# briefing pin-context chain `project:<id>` built in session_briefing),
# and a procedural entry's scope MUST equal its pin-context to be matched
# by the pin chain — so the entry scope grammar has to admit the same id
# shape. Forbidding `/` here made every project-scoped case assignment 500
# at ProceduralEntryCreate even though the pin-context used it freely.
SCOPE_PATTERN = re.compile(
    r'^(global|project:[A-Za-z0-9._/-]+|app:[A-Za-z0-9._/-]+(:[A-Za-z0-9._/-]+)?)$'
)

# Anchor verb/context grammar — mirrors the KV procedure-key grammar
# (kv_utils.py) the 046 migration established; the design relocates that
# taxonomy into columns (§18.2).
ANCHOR_LABEL_PATTERN = re.compile(r'^[a-z][a-z0-9_-]*$')


def validate_scope_label(value: str, *, field_name: str = 'scope') -> str:
    """Validate a scope / pin-context label against the scope grammar.

    Raises ``ValueError`` with an actionable message. ``user`` is called
    out explicitly because it is a *valid KV scope* and the most likely
    incorrect carry-over.
    """
    candidate = value.strip()
    if not SCOPE_PATTERN.match(candidate):
        hint = ''
        if candidate == 'user' or candidate.startswith('user:'):
            hint = (
                ' Procedures/strategies have no user scope — they are shared '
                'knowledge. Per-user briefing curation is done by pinning '
                'entries into a pin-chain context, not by scoping the entry.'
            )
        raise ValueError(
            f'{field_name} {candidate!r} does not match the scope grammar '
            f'(global | project:<id> | app:<id>).{hint}'
        )
    return candidate


# --- core entry DTOs ------------------------------------------------------


class ProceduralSourceDTO(BaseModel):
    """A single source edge attached to an procedural entry."""

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


class ProceduralPinDTO(BaseModel):
    """A context-binding pin that anchors an entry into a pin chain."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    context_key: ShortLabel
    entry_id: UUID
    position: int = Field(ge=0)
    pinned_by: str | None = None
    created_at: dt.datetime


class ProceduralEntryDTO(BaseModel):
    """Public-facing representation of an procedural entry.

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
    # Phase 2 outcome counters (§18.5). Cards render raw counts (e.g. 9/11 ✓),
    # never a bare rate; ranking uses the Beta-Bernoulli posterior.
    success_count: int = 0
    failure_count: int = 0
    mixed_count: int = 0
    uses: int = 0
    last_used_at: dt.datetime | None = None
    supersedes_id: UUID | None = None
    superseded_by_id: UUID | None = None
    published_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    # Lineage / pointer fields surfaced for agent readability.
    sources: list[ProceduralSourceDTO] = Field(default_factory=list)
    pins: list[ProceduralPinDTO] = Field(default_factory=list)


# --- mutation DTOs --------------------------------------------------------


class ProceduralEntryCreate(BaseModel):
    """Create a new procedural entry.

    Anchor shapes (§18.1) are enforced here so a malformed write fails at
    the DTO boundary, not at the DB CHECK:

    * procedure — ``verb`` AND ``context`` required.
    * strategy — ``verb`` required, ``context`` FORBIDDEN (a strategy is
      the projection over all procedures sharing (scope, verb)).

    ``trigger`` (when_to_use / when_to_apply) is required for every write:
    it is the retrieval key (spike §19.1).
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
    status: StatusLiteral = (
        'published'  # design: new writes default published (visible to search/briefing)
    )
    origin: OriginLiteral = 'manual'
    supersedes_id: UUID | None = None

    @model_validator(mode='after')
    def _validate_anchor_shape(self) -> 'ProceduralEntryCreate':
        validate_scope_label(self.scope)
        if self.kind == 'procedure':
            if not self.verb or not self.context:
                raise ValueError(
                    'procedure entries require both verb and context '
                    '(anchor ≡ (scope, verb, context); §18.1)'
                )
        elif self.kind == 'strategy':
            if not self.verb:
                raise ValueError('strategy entries require a verb (anchor ≡ (scope, verb))')
            if self.context:
                raise ValueError(
                    'strategy entries must NOT set context — a strategy groups '
                    'all procedures sharing (scope, verb) (§18.1)'
                )
        for label_name in ('verb', 'context'):
            label = getattr(self, label_name)
            if label is not None and not ANCHOR_LABEL_PATTERN.match(label):
                raise ValueError(
                    f'{label_name} {label!r} must match ^[a-z][a-z0-9_-]*$ '
                    '(the anchor-label grammar; §18.2)'
                )
        if not (self.trigger or '').strip():
            raise ValueError(
                'trigger is required: it is the when_to_use / when_to_apply '
                'phrase that retrieval anchors on (§6, spike §19.1)'
            )
        return self


class ProceduralEntryUpdate(BaseModel):
    """Mutate an existing entry in place.

    All fields are optional; only set fields are updated. The repository
    appends a new ``procedural_entry_versions`` row on every successful
    update — see design §3.2.
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


class ProceduralSourceCreate(BaseModel):
    """Attach a source edge to an entry."""

    model_config = ConfigDict(extra='forbid')

    source_entry_id: UUID | None = None
    source_note_id: UUID | None = None
    source_memory_unit_id: UUID | None = None
    role: SourceRoleLiteral = 'evidence'
    weight: float = Field(default=1.0, ge=0.0, le=10.0)


class ProceduralPinCreate(BaseModel):
    """Pin an entry into a context-binding chain.

    Context keys reuse the scope grammar plus the Hermes per-agent form
    ``app:hermes:<agent_identity>`` (§19.8). No ``user`` context.
    ``position=None`` appends to the end of the context's chain.
    """

    model_config = ConfigDict(extra='forbid')

    context_key: ShortLabel
    entry_id: UUID
    position: int | None = Field(default=None, ge=0)
    pinned_by: str | None = None

    @model_validator(mode='after')
    def _validate_context_key(self) -> 'ProceduralPinCreate':
        validate_scope_label(self.context_key, field_name='context_key')
        return self


class ProceduralEntryVersionDTO(BaseModel):
    """One row of the uncapped version ledger (diff / rollback surface).

    Each successful ``update`` / ``upsert`` rewrite appends a snapshot;
    rollback is a read of an old snapshot + a new version write — never
    destructive (§18.8 / §19.8).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    entry_id: UUID
    version: int
    title: str
    summary: str
    body: str = ''
    trigger: str | None = None
    tags: list[str] = Field(default_factory=list)
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    edited_by: str | None = None
    edit_reason: str | None = None
    created_at: dt.datetime


# --- case submission DTOs ---------------------------------------------------
#
# A case is a NOTE (role='case') in the hidden `procedural` system vault —
# §5.1 / §18.3 / §18.9.0. These DTOs are the case_submit wire shape; the
# note itself is composed server-side from the episode template.


class CaseSubmit(BaseModel):
    """Submit a worked episode as a case (§5.1 template, §18.9.0 vault).

    ``case_of`` is the PRIMARY assignment API (§18.1): the agent that just
    enacted a procedure knows which one it was. Without it, the server
    runs the assignment judge (§19.3); ``separation != 'clean'`` lands in
    the lint queue (file-then-lint, decision #5) — never an in-session
    clarification round-trip.
    """

    model_config = ConfigDict(extra='forbid')

    title: ShortLabel
    trigger: str = Field(
        min_length=1,
        description='What kicked the episode off — embedded for case↔procedure matching.',
    )
    situation: str = Field(
        default='',
        description='Context going in (prior state, constraints).',
    )
    actions: list[str] = Field(
        default_factory=list,
        description='Ordered actions taken, one step per item.',
    )
    outcome: Literal['success', 'failure', 'mixed']
    lesson: str = Field(
        default='',
        description='What to do differently / confirm next time.',
    )
    project_id: str | None = Field(
        default=None,
        description='Provenance — recorded in doc_metadata, NOT a vault binding (§18.9.0).',
    )
    case_of: UUID | None = Field(
        default=None,
        description='Procedural entry this case instantiates (explicit assignment).',
    )
    submitted_by: str | None = Field(
        default=None,
        description='Submitting app/agent identity (provenance).',
    )
    tags: list[str] = Field(default_factory=list)


class CaseAssignment(BaseModel):
    """How the submitted case was routed to the procedural plane."""

    mode: Literal[
        'explicit',  # caller supplied case_of
        'auto_assigned',  # judge: instance_of + separation=clean
        'new_procedure_draft',  # judge: new_procedure + separation=clean → draft anchor
        'escalated',  # separation != clean OR judge unavailable → lint queue
        'skipped',  # assignment disabled / nothing to assign against
    ]
    entry_id: UUID | None = None
    finding_id: UUID | None = None
    decision: str | None = None
    separation: str | None = None
    reasoning: str | None = None


class CaseSubmitResult(BaseModel):
    """Result envelope for a case submission."""

    note_id: UUID
    vault_id: UUID
    assignment: CaseAssignment


# --- derivation queue DTOs ------------------------------------------------


class ProceduralDerivationQueueDTO(BaseModel):
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


class ProceduralDerivationQueueClaim(BaseModel):
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


class ProceduralSearchRequest(BaseModel):
    """Hybrid BM25 + vector search across the procedural plane.

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
    vault_id: UUID | None = Field(
        default=None,
        description=(
            'Optional vault UUID to scope the search. When set, every '
            'BM25, vector, and pin-chain candidate is restricted to '
            'this vault — the multi-tenancy guardrail that prevents a '
            'vault-A caller from reading vault-B entries by accident. '
            'A non-null value here is the expected production call '
            'shape; leaving it None retains the cross-vault result '
            'set for operator/CLI paths that need a global view.'
        ),
    )


class ProceduralSearchHit(BaseModel):
    """A single hit from the procedural search service."""

    model_config = ConfigDict(from_attributes=True)

    entry: ProceduralEntryDTO
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


class ProceduralSearchResponse(BaseModel):
    """Result envelope for an procedural-plane search."""

    hits: list[ProceduralSearchHit] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False
    took_ms: float = 0.0


class ProceduralBriefingCard(BaseModel):
    """A single card in the session briefing's procedural slot.

    The briefing renders one card per entry with: title, kind badge, a
    truncated summary, and the matched trigger / scope. The agent decides
    whether to expand any card on demand.
    """

    model_config = ConfigDict(from_attributes=True)

    entry: ProceduralEntryDTO
    pin_position: int
    context_key: ShortLabel


class ProceduralBriefingCards(BaseModel):
    """Envelope for the briefing surface — the cards pre-sorted by pin order."""

    cards: list[ProceduralBriefingCard] = Field(default_factory=list)
    context_keys: list[ShortLabel] = Field(
        default_factory=list,
        description='The pin contexts that contributed entries to this briefing.',
    )
    total_pinned: int = 0


__all__ = [
    'ANCHOR_LABEL_PATTERN',
    'CaseAssignment',
    'CaseSubmit',
    'CaseSubmitResult',
    'DerivationQueueStatus',
    'DerivationStatusLiteral',
    'ProceduralBriefingCard',
    'ProceduralBriefingCards',
    'ProceduralDerivationQueueClaim',
    'ProceduralDerivationQueueDTO',
    'ProceduralEntryCreate',
    'ProceduralEntryDTO',
    'ProceduralEntryUpdate',
    'ProceduralEntryVersionDTO',
    'ProceduralKind',
    'ProceduralOrigin',
    'ProceduralPinCreate',
    'ProceduralPinDTO',
    'ProceduralSearchHit',
    'ProceduralSearchRequest',
    'ProceduralSearchResponse',
    'ProceduralSourceCreate',
    'ProceduralSourceDTO',
    'ProceduralSourceRole',
    'ProceduralStatus',
    'KindLiteral',
    'OriginLiteral',
    'SCOPE_PATTERN',
    'ShortLabel',
    'SourceRoleLiteral',
    'StatusLiteral',
    'validate_scope_label',
]
