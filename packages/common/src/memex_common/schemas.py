"""Custom models for Memex used internally for note management and retrieval."""

import datetime as dt
import logging
import re
from enum import Enum
from typing import Any, Annotated, Literal
from uuid import UUID
from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    field_serializer,
    BeforeValidator,
    ConfigDict,
    model_validator,
)
import base64
import binascii

from memex_common.types import MemexTypes, FactTypes


_logger = logging.getLogger('memex.common.schemas')
from memex_common.mixins import VaultMixin


# Variance of Uniform(0, 1) = Beta(1, 1), the cold-start posterior
# variance ceiling. Mirrors ``memex_core.memory.confidence.MAX_VARIANCE``;
# duplicated here because ``memex_common`` MUST NOT depend on
# ``memex_core`` (the dependency direction is core → common). The
# cross-reference unit test in
# ``packages/core/tests/unit/memory/test_confidence.py`` (see
# ``TestDtoFormulaConsistency``) pins equivalence between this constant
# and the formula in ``memex_core.memory.confidence.mean_and_variance``.
# Extracted from inline ``1.0 / 12.0`` literals so
# the Field constraint and the validator formula reference a single
# source of truth.
# Also defined in
# ``memex_core.memory.confidence.MAX_VARIANCE`` and
# ``memex_common.config._MAX_VARIANCE``. Any edit here MUST be mirrored
# in both of those locations. Future: a ``memex_common.constants``
# module would reduce duplication from 3 to 2 (core can't import
# common for the reverse direction), halving the drift surface.
_MAX_VARIANCE: float = 1.0 / 12.0


def decode_base64(v: Any) -> bytes:
    """Validate and return Base64 encoded bytes."""
    if isinstance(v, str):
        v = v.encode('utf-8')

    if isinstance(v, bytes):
        try:
            # Check if it is valid base64 by trying to decode it
            # We don't return the decoded value, we return the original v
            base64.b64decode(v, validate=True)
            return v
        except binascii.Error as e:
            raise ValueError(f'Value is not valid Base64 encoded data: {e}')
    return v


Base64Bytes = Annotated[bytes, BeforeValidator(decode_base64)]


class EntityType(str, Enum):
    """Semantic type of an entity in the knowledge graph."""

    PERSON = 'Person'
    ORGANIZATION = 'Organization'
    LOCATION = 'Location'
    CONCEPT = 'Concept'
    TECHNOLOGY = 'Technology'
    FILE = 'File'
    MISC = 'Misc'


class IntentClass(str, Enum):
    """Lifecycle class for a memory unit, set at write time.

    permanent — identity / preferences / facts that should never decay.
    durable   — project decisions, multi-week relevance (default).
    ephemeral — task context, days-to-weeks relevance only.
    """

    PERMANENT = 'permanent'
    DURABLE = 'durable'
    EPHEMERAL = 'ephemeral'


class RiskClass(str, Enum):
    """Content sensitivity, set at write time.

    none      — public-safe content (default).
    sensitive — flagged for linter review; still retrievable in default scope.
    private   — excluded from default retrieval; surfaced only on explicit query.
    safety    — refused at ingestion (Memex will not persist).
    """

    NONE = 'none'
    SENSITIVE = 'sensitive'
    PRIVATE = 'private'
    SAFETY = 'safety'


VALID_INTENT_CLASSES: frozenset[str] = frozenset(c.value for c in IntentClass)
VALID_RISK_CLASSES: frozenset[str] = frozenset(c.value for c in RiskClass)


# Canonical ``Literal`` aliases for use as type annotations across packages
# (MCP tool params, DSPy classifier signatures, etc.). mypy requires
# ``Literal[...]`` arguments to be compile-time string literals — we cannot
# unpack a runtime expression like ``Literal[*tuple(c.value for c in IntentClass)]``
# without losing static-type narrowing — so the values are still hand-listed here.
# However, divergence between these aliases and the ``IntentClass`` /
# ``RiskClass`` enums is caught at test time (see
# ``packages/common/tests/test_enum_literal_parity.py``) which asserts
# ``typing.get_args(IntentLiteral) == tuple(c.value for c in IntentClass)``.
# This collapses three+ duplicate definitions (MCP server, DSPy classifier,
# Hermes JSON schema) into ONE canonical definition; downstream importers
# reference these names instead of re-typing the value tuple.
IntentLiteral = Literal['permanent', 'durable', 'ephemeral']
RiskLiteral = Literal['none', 'sensitive', 'private', 'safety']


# Shared default-on-fail coercion. Both ``RawFact`` (LLM output) and
# ``ExtractedFact`` (downstream pipeline) absorb LLM-produced strings that
# may be malformed (omitted fields, unknown values, non-string garbage like
# None / int / list / dict). The "extraction must never be blocked by a
# classification mishap" invariant means we always coerce to the schema
# default rather than raise. Keep the coercion logic in
# one place so a future addition to ``IntentClass`` / ``RiskClass`` cannot
# silently desync the two pydantic ``@field_validator``s on the fact models.


def coerce_intent_class(v: object) -> str:
    """Coerce ``v`` to a valid ``IntentLiteral`` string, defaulting to ``durable``."""
    if isinstance(v, str) and v in {c.value for c in IntentClass}:
        return v
    return IntentClass.DURABLE.value


def coerce_risk_class(v: object) -> str:
    """Coerce ``v`` to a valid ``RiskLiteral`` string, defaulting to ``none``."""
    if isinstance(v, str) and v in {c.value for c in RiskClass}:
        return v
    return RiskClass.NONE.value


class LineageDirection(str, Enum):
    """Direction of lineage traversal."""

    UPSTREAM = 'upstream'
    DOWNSTREAM = 'downstream'
    BOTH = 'both'


class LineageResponse(BaseModel):
    """Recursive schema for lineage response."""

    entity_type: str = Field(description='The type of the entity (mental_model, observation, etc.)')
    entity: dict[str, Any] = Field(description='The full DTO of the entity.')
    derived_from: list['LineageResponse'] = Field(
        default_factory=list,
        description='List of entities this entity is derived from (upstream) or that are derived from it (downstream).',
    )


LineageResponse.model_rebuild()


class NoteMetadata(BaseModel):
    """A markdown note stored in the file system. Can be generated by the user or an LLM."""

    # NB: may need to be moved
    _manifest_path: str | None = PrivateAttr(default=None)

    date_created: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc),
        description='The timestamp when the note was created',
    )

    ## These fields can be updated **after** the note metadata is initialized
    uuid: str | None = Field(
        default=None,
        description='The unique identifier of the note (all files in the note share this UUID)',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )
    name: str | None = Field(
        default=None, description='The name of the note', examples=['helpful-assistant']
    )
    files: list[str] | None = Field(
        default=None,
        description='List of files associated with the note',
        examples=[
            ['ROLE.md'],
            ['skills/helpful-assistant/SKILL.md', 'skills/helpful-assistant/scripts/utils.py'],
        ],
    )
    description: str | None = Field(
        default=None,
        description='A summary of the entire note, answering Who, What, When, Where, Why, How',
        examples=['A helpful role that assists users in managing tasks.'],
    )
    author: str | None = Field(
        default=None,
        description='Name of the author who created the entity.',
        examples=['user', 'claude opus', 'gemini 3'],
    )
    etag: str | None = Field(
        default=None,
        description="The MD5 hash of the note's description file (e.g. NOTE.md)",
        examples=['9e107d9d372bb6826bd81d3542a419d6'],
    )
    tags: list[str] | None = Field(
        default=None,
        description='List of tags associated with the note',
        examples=[['assistant', 'helpful', 'task-management']],
    )
    type: MemexTypes | None = Field(
        default=None,
        description='The type of the Memex entity',
    )

    # NB: this needs to be done because IndexEntry types can be string or None
    def model_post_init(self, __context) -> None:
        # Mypy complains
        self.type: str | None
        if self.type is None:
            self.type = MemexTypes.NOTE

    @field_serializer('type')
    def serialize_type(self, type: MemexTypes | None) -> str | None:
        if type is None:
            return None
        else:
            return type.value

    @field_serializer('date_created')
    def serialize_date_created(self, date_created: dt.datetime) -> str:
        return date_created.isoformat()

    def update(self, key: str, value: list[float] | list[str] | str | None):
        setattr(self, key, value)


class RetainContent(VaultMixin):
    """Input context for content retention."""

    content: str = Field(..., description='The content to be retained.')
    event_date: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(tz=dt.timezone.utc),
        description='The date of when the content was created or relevant.',
    )
    context: str | None = Field(default=None, description='Additional context for the content.')
    payload: dict[str, Any] = Field(
        default_factory=dict, description='Additional metadata for the content.'
    )


class RetrievalRequest(BaseModel):
    """
    Unified request object for memory retrieval.

    NOTE — dual-model architecture (intentional):
    There are TWO ``RetrievalRequest`` classes in the codebase:

    1. ``memex_common.schemas.RetrievalRequest`` (this class) — the
       **wire / protocol** model. It is the public Pydantic schema used by
       the FastAPI server (request body), the ``RemoteMemexAPI`` HTTP client,
       and the OpenAPI spec. Enum-typed fields (``intent_class: IntentClass``,
       ``risk_class: RiskClass``) act as a self-documenting contract for
       external callers.
    2. ``memex_core.memory.retrieval.models.RetrievalRequest`` — the
       **internal / storage** model (SQLModel). It is built inside the
       ``SearchService`` from already-validated primitives, so it carries
       ``intent_class: str | None`` / ``risk_class: str | None`` and
       enforces the same value set via a ``model_validator`` against the
       canonical ``VALID_INTENT_CLASSES`` / ``VALID_RISK_CLASSES`` frozensets.

    The boundary conversion happens in
    ``memex_core/server/retrieval.py::search_memories`` which unpacks the
    enum via ``request.intent_class.value if request.intent_class else None``
    before calling ``MemexAPI.search``.

    If you need to add or rename a class value, update ``IntentClass`` /
    ``RiskClass`` in this module — both ``VALID_INTENT_CLASSES`` (here) and
    the model_validator in the core model derive from those enums.
    """

    query: str = Field(..., description='The search query or context string.')
    limit: int = Field(default=10, description='Maximum number of results to return.')
    offset: int = Field(default=0, description='Number of results to skip.')
    token_budget: int | None = Field(
        default=None, description='Maximum token budget for results (greedy packing).'
    )

    # Scoping
    vault_ids: list[UUID | str] | None = Field(
        default=None,
        description='List of specific vault IDs or names to search. If None or empty, searches ALL vaults.',
    )
    filters: dict[str, Any] = Field(
        default_factory=dict, description='Optional key-value filters (e.g. fact_type).'
    )

    # Temporal filtering
    after: dt.datetime | None = Field(
        default=None, description='Only return results after this date (ISO 8601).'
    )
    before: dt.datetime | None = Field(
        default=None, description='Only return results before this date (ISO 8601).'
    )

    # Tag filtering
    tags: list[str] | None = Field(
        default=None, description='Only return results from notes with ALL of these tags.'
    )

    # Temporal concretization
    reference_date: dt.datetime | None = Field(
        default=None,
        description=(
            'Reference point for resolving relative temporal expressions '
            '(e.g. "last week"). Defaults to now (UTC) when None.'
        ),
    )

    # Source context filtering
    source_context: str | None = Field(
        default=None,
        description=(
            'Filter MemoryUnits by their context field (e.g. "user_notes"). '
            'When set, only units with matching context are returned.'
        ),
    )

    # Intent / risk class filtering (write-time classifier)
    intent_class: IntentClass | None = Field(
        default=None,
        description=(
            'Filter MemoryUnits by intent_class (permanent | durable | ephemeral). '
            'None disables the filter.'
        ),
    )
    risk_class: RiskClass | None = Field(
        default=None,
        description=(
            'Filter MemoryUnits by risk_class (none | sensitive | private | safety). '
            'None disables the filter.'
        ),
    )

    # Query expansion
    expand_query: bool = Field(
        default=False,
        description='Whether to expand the query using LLM-generated semantic variations.',
    )

    # Advanced options
    rerank: bool = Field(
        default=True, description='Whether to apply neural reranking if available.'
    )
    min_score: float | None = Field(
        default=None, description='Minimum score threshold for results (post-reranking).'
    )
    strategy_weights: dict[str, float] | None = Field(
        default=None, description='Optional custom weights for RRF fusion.'
    )
    strategies: list[str] | None = Field(
        default=None,
        description=(
            'Inclusion list of strategies to run. '
            'Valid values: semantic, keyword, graph, temporal, mental_model. '
            'If None, all strategies are used.'
        ),
    )
    include_vectors: bool = Field(
        default=False, description='Whether to include embeddings in the result (slower).'
    )
    include_stale: bool = Field(
        default=False, description='Whether to include stale memory units in results.'
    )
    include_superseded: bool = Field(
        default=False,
        description='Whether to include superseded (low-confidence) memory units in results.',
    )
    include_deprioritized: bool = Field(
        default=False,
        description='Whether to include deprioritized memory units in results.',
    )
    apply_pre_filter: bool = Field(
        default=True,
        description=(
            'pre-reranker MW/FSFM filter at hydration. Default ON drops obviously-failed '
            'or decayed candidates before the cross-encoder runs (~30% reranker latency '
            'reduction with cold-start safeguards intact). Set False for historical / audit / '
            'lineage queries that need to see contradicted, behaviorally-failed, or decayed '
            'units — every branch (MW + FSFM + future confidence) is bypassed in one go.'
        ),
    )
    debug: bool = Field(
        default=False,
        description=(
            'When True, include per-result strategy attribution (name, rank, RRF score, timing).'
        ),
    )


class StrategyDebugInfo(BaseModel):
    """Per-strategy attribution for a single retrieval result."""

    strategy_name: str = Field(description='Name of the strategy that contributed this result.')
    rank: int = Field(description='Rank of this result within the strategy (1-based).')
    rrf_score: float = Field(description='RRF contribution score from this strategy.')
    raw_score: float | None = Field(
        default=None, description='Raw score from the strategy (distance, BM25, etc.).'
    )
    timing_ms: float | None = Field(
        default=None, description='Time taken by this strategy in milliseconds.'
    )


class ReflectionRequest(VaultMixin):
    """
    Request to run the reflection loop on a specific entity.
    """

    entity_id: UUID = Field(description='The UUID of the entity to reflect upon.')
    limit_recent_memories: int | None = Field(
        default=20,
        description=(
            'Number of recent memories to consider. None means no per-request cap '
            "('full' scope); the engine still enforces MAX_FULL_SCOPE_UNITS=1000."
        ),
    )


class IngestURLRequest(VaultMixin):
    """Request to ingest content from a URL."""

    url: str = Field(..., description='The URL to ingest.')
    reflect_after: bool = Field(
        default=True, description='Whether to run reflection after ingestion.'
    )
    assets: dict[str, str] = Field(
        default_factory=dict,
        description='Optional dictionary of assets (filename -> base64 content).',
    )
    user_notes: str | None = Field(
        default=None,
        description='Optional user-provided context or commentary to include in the note.',
    )


class IngestFileRequest(VaultMixin):
    """Request to ingest content from a local file path (server-side)."""

    file_path: str = Field(..., description='The absolute path to the file on the server.')
    reflect_after: bool = Field(
        default=True, description='Whether to run reflection after ingestion.'
    )
    user_notes: str | None = Field(
        default=None,
        description='Optional user-provided context or commentary to include in the note.',
    )


class CreateVaultRequest(BaseModel):
    """Request to create a new vault."""

    name: str = Field(..., description='The name of the vault.')

    description: str | None = Field(default=None, description='Optional description.')


class MemoryUnitBase(VaultMixin):
    """Base schema for Memory Unit shared between DTOs and SQLModels."""

    text: str = Field(description='The textual content of the memory unit.')
    fact_type: FactTypes = Field(description='The type/category of the memory unit.')
    status: str = Field(
        default='active',
        description='Content status: active or stale.',
        examples=['active', 'stale'],
    )

    intent_class: IntentClass = Field(
        default=IntentClass.DURABLE,
        description='Lifecycle class set at write time (permanent | durable | ephemeral).',
    )
    risk_class: RiskClass = Field(
        default=RiskClass.NONE,
        description='Content sensitivity set at write time (none | sensitive | private | safety).',
    )

    mentioned_at: dt.datetime | None = Field(
        default=None, description='The datetime when the memory unit was mentioned.'
    )
    occurred_start: dt.datetime | None = Field(
        default=None, description='The start datetime of when the fact/event occurred.'
    )
    occurred_end: dt.datetime | None = Field(
        default=None, description='The end datetime of when the fact/event occurred.'
    )


class SupersessionInfo(BaseModel):
    """Info about a unit that supersedes this one."""

    unit_id: UUID
    unit_text: str
    note_title: str | None = None
    relation: str  # 'contradicts' | 'weakens'


class MemoryUnitDTO(MemoryUnitBase):
    """DTO for a Memory Unit (Fact/Experience).

    PYDANTIC-ONLY: this class deliberately does
    NOT inherit from ``SQLModel`` and is never used for DDL / table
    creation. The persistent table definition lives on
    ``memex_core.memory.sql_models.MemoryUnit``, which carries the
    ``sa_column=Column(Integer, ...)`` declarations for the
    ``confidence_evidence_count`` (and friends) columns. The DTO
    fields here therefore use plain ``Field(..., default=...)`` —
    omitting ``sa_column`` is correct, not a bug. If a future change
    promotes this DTO to a SQLModel ``table=True`` class, every
    column-bearing field MUST gain a corresponding ``sa_column``
    declaration to avoid Pydantic-type-inference creating a VARCHAR
    where INTEGER is required.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(
        description='The unique identifier of the memory unit.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )

    note_id: UUID | None = Field(
        default=None,
        description='The unique identifier of the source note.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )

    chunk_id: UUID | None = Field(
        default=None,
        description='The source chunk ID this memory unit was extracted from.',
    )

    node_ids: list[str] = Field(
        default_factory=list,
        description='Page-index node IDs linked to the source chunk.',
    )

    source_note_ids: list[UUID] = Field(
        default_factory=list,
        description='List of source note IDs.',
        examples=[['123e4567-e89b-12d3-a456-426614174000']],
    )

    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description='Additional metadata associated with the memory unit.',
        examples=[{'source': 'interview', 'author': 'user'}],
    )

    score: float | None = Field(
        default=None,
        description='Relevance score from retrieval or reranking (0.0 to 1.0).',
        examples=[0.95],
    )

    debug_info: list[StrategyDebugInfo] | None = Field(
        default=None,
        description='Per-strategy attribution when debug=True. None when debug is off.',
    )

    confidence: float = Field(
        default=1.0,
        description='Confidence score (0.0-1.0). VALIDATOR ORDERING: the '
        '``_compute_confidence_variance`` after-validator '
        'clamps out-of-range values via ``object.__setattr__``. Adding a '
        '``ge=0.0, le=1.0`` field constraint here would cause Pydantic to '
        'REJECT ``1.0001`` at field-validation time (before the after-validator '
        'runs), turning the current defence-in-depth clamp into a hard '
        'failure. If a field-level constraint is required, the after-validator '
        'must be promoted to a ``mode="before"`` validator first.',
    )

    confidence_evidence_count: int = Field(
        default=0,
        ge=0,
        description='Negative-evidence event count (number of contradicts/weakens '
        'links pointing at this unit). Cold-start = 0; bumped on each weaken/contradict '
        'step in the contradiction engine. UPPER BOUND: no '
        '``le`` constraint is set — the DB column is ``INTEGER NOT NULL`` so values '
        'up to ``2**31 - 1`` are legal at the storage layer. The Beta variance '
        'formula ``alpha = 1 + confidence * evidence_count`` performs float '
        'multiplication, so integer precision degrades above ``2**53`` (the float64 '
        'mantissa cap). In practice this ceiling is unreachable — a single unit '
        'would need 9 quadrillion contradicting links — so no validator is added; '
        'documenting the threshold here is the right tradeoff.',
    )

    confidence_variance: float = Field(
        default=_MAX_VARIANCE,
        ge=0.0,
        le=_MAX_VARIANCE,
        description='Closed-form Beta(1, 1) posterior variance derived at '
        'hydration from (confidence, confidence_evidence_count). Range [0, 1/12]. '
        'Cold-start (count=0) lands at 1/12 (max); shrinks toward 0 as evidence '
        'accumulates. Lower variance = more supporting evidence = more trustworthy. '
        'COMPUTED-ONLY: any value passed via constructor kwarg is replaced by the '
        '``_compute_confidence_variance`` model validator. '
        'Do not pass ``confidence_variance=...`` expecting it to be respected — '
        'set ``confidence`` and ``confidence_evidence_count`` instead. '
        'BELT-AND-SUSPENDERS: the ``ge=0.0, le=_MAX_VARIANCE`` '
        'constraints here are NOT enforced for the normal construction path — the '
        '``_compute_confidence_variance`` after-validator overwrites the field via '
        '``object.__setattr__``, which bypasses Pydantic field validation. The '
        'constraints fire only for ``model_validate`` / ``model_construct`` paths '
        'that skip the after-validator. Kept for documentation + the explicit '
        'in-validator bounds check on the computed value.',
    )

    superseded_by: list[SupersessionInfo] | None = Field(
        default=None,
        description='Units that supersede this one.',
    )

    success_co_count: int = Field(
        default=0,
        description='MW success co-occurrence counter.',
    )

    failure_co_count: int = Field(
        default=0,
        description='MW failure co-occurrence counter.',
    )

    is_deprioritized: bool = Field(
        default=False,
        description='Whether this unit has been deprioritized (non-destructive retrieval downweight).',
    )

    @model_validator(mode='after')
    def _compute_confidence_variance(self) -> 'MemoryUnitDTO':
        """Derive confidence_variance from (confidence, evidence_count).

        Closed-form Beta(1, 1) posterior variance — kept inline here (rather
        than importing memex_core.memory.confidence) to avoid memex_common
        depending on memex_core. The formula MUST stay in lockstep with
        ``memex_core.memory.confidence.mean_and_variance`` — guarded by the
        cross-reference unit test in ``test_confidence.py``.

        Formula duplication: extracting the pure
        formula to a shared ``memex_common`` helper would eliminate the
        duplication, but it's deliberately kept duplicated in v1 to avoid
        widening the ``memex_common`` surface for one tiny formula. The
        cross-reference test pins the equivalence per release; promote to
        a shared helper only if a third call site appears.

        Input validation: ``confidence`` must be in
        ``[0, 1]`` for the Beta(α, β) shape parameters to stay non-negative.
        We clamp at the top of the validator as a first-class guard so a
        single out-of-range input (an upstream bug writing ``1.0001``)
        cannot produce a negative ``beta`` and a nonsensical variance —
        rather than relying on the post-hoc bounds check below to catch
        it after the formula has already evaluated. The clamp mirrors
        the contradiction engine's SQL-level ``GREATEST(0, LEAST(1, …))``
        on writes.

        Defence-in-depth: ``confidence_variance`` has a
        ``ge=0.0, le=1/12`` field constraint, but ``object.__setattr__``
        bypasses Pydantic validation. The bounds check below stays as a
        belt-and-suspenders guard against future formula drift.
        """
        # Clamp at the top — defends against upstream input bugs without
        # surprising the caller with a hard failure for benign drift.
        # Also write the clamped value back to
        # ``self.confidence`` so the DTO's ``confidence`` and
        # ``confidence_variance`` fields are mutually consistent for any
        # downstream consumer that reads both. Without this, a caller
        # could see ``confidence=1.0001`` paired with a variance computed
        # from ``confidence=1.0`` — a silent inconsistency.
        confidence_clamped = max(0.0, min(1.0, self.confidence))
        if confidence_clamped != self.confidence:
            # Emit a debug-level diagnostic when the
            # clamp engages so an upstream bug writing out-of-range
            # confidence is observable in logs instead of silently
            # absorbed. Debug-level keeps the hot path quiet on the
            # happy path; flip to warning if calibration shows the
            # clamp firing on real traffic.
            _logger.debug(
                'DTO clamp engaged: confidence %r → %r '
                '(out of [0, 1] range — likely upstream write bug)',
                self.confidence,
                confidence_clamped,
            )
            object.__setattr__(self, 'confidence', confidence_clamped)
        alpha = 1.0 + confidence_clamped * self.confidence_evidence_count
        beta = 1.0 + (1.0 - confidence_clamped) * self.confidence_evidence_count
        n = alpha + beta
        variance = (alpha * beta) / (n * n * (n + 1.0))
        if not (0.0 <= variance <= _MAX_VARIANCE):
            raise ValueError(
                f'Invariant violated: computed confidence_variance={variance!r} '
                f'is outside [0, 1/12] (confidence={self.confidence!r}, '
                f'evidence_count={self.confidence_evidence_count!r}). '
                f'This indicates either a formula drift from '
                f'memex_core.memory.confidence.mean_and_variance or an '
                f'out-of-range confidence input.'
            )
        # ``object.__setattr__`` bypasses Pydantic's validation pipeline:
        # any future ``@field_validator('confidence_variance')`` (or
        # ``Field(..., ge=..., le=...)`` re-tightening) will NOT fire on
        # this assignment. The bounds check immediately above is the only
        # guard that runs here — do not remove it on the assumption that
        # a field-level validator covers it.
        #
        # Computed-field alternative: Pydantic v2's
        # ``@computed_field`` would make this derivation declarative and
        # remove the ``object.__setattr__`` escape hatch, BUT it removes
        # the field from ``model_fields`` and changes serialization /
        # SQLModel column generation — non-trivial for ``MemoryUnitDTO``
        # which is consumed by clients that introspect field shapes. The
        # validator + ``__setattr__`` pattern is the conservative
        # equivalent. Promote to ``@computed_field`` if/when Pydantic v2
        # ships a "computed but still in model_fields" mode.
        object.__setattr__(self, 'confidence_variance', variance)
        return self

    @property
    def enriched_text(self) -> str:
        """Text with date metadata for LLM consumption."""
        date = self.mentioned_at or self.occurred_start
        date_str = date.strftime('%Y-%m-%d') if date else 'Unknown'
        return f'[{date_str}] {self.text}'


class ObservationDTO(BaseModel):
    """DTO for an Observation (Mental Model component)."""

    id: UUID | None = Field(
        default=None,
        description='The unique identifier of the observation.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )
    title: str = Field(
        description='Short title or headline for the observation.',
        examples=['User prefers Python over Java'],
    )

    content: str = Field(
        description='Detailed content of the observation.',
        examples=['The user consistently chooses Python for new projects due to its simplicity.'],
    )

    trend: str | None = Field(
        default=None,
        description='Current trend of this observation (e.g. new, stable, strengthening).',
        examples=['strengthening'],
    )

    evidence: list[dict[str, Any]] = Field(
        default_factory=list,
        description='List of supporting evidence items.',
        examples=[{'memory_id': '...', 'quote': 'I love Python'}],
    )


class EntityDTO(BaseModel):
    """DTO for an Entity."""

    id: UUID = Field(
        description='The unique identifier of the entity.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )

    name: str = Field(
        description='The canonical name of the entity.',
        examples=['Python'],
    )

    mention_count: int = Field(
        default=0,
        description='Cumulative number of times this entity has been mentioned.',
        examples=[42],
    )

    vault_id: UUID | None = Field(
        default=None,
        description='The UUID of the vault this entity belongs to.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )

    entity_type: str | None = Field(
        default=None,
        description='The semantic type of the entity (e.g. Person, Organization, Concept).',
        examples=['Person', 'Technology'],
    )

    metadata: dict[str, Any] = Field(
        default={},
        description='Vault-scoped metadata derived from reflection (description, category).',
    )


class VaultDTO(BaseModel):
    """DTO for a Vault."""

    id: UUID = Field(
        description='The unique identifier of the vault.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )

    name: str = Field(
        description='The name of the vault.',
        examples=['Personal', 'Work'],
    )

    description: str | None = Field(
        default=None,
        description='Optional description of the vault.',
        examples=['My personal memories and notes.'],
    )

    mw_mode: str = Field(
        default='stationary',
        description='MW mode for the vault: "stationary" or "ema".',
    )

    is_active: bool = Field(
        default=False,
        description='Whether this vault is the currently active (writer) vault.',
    )

    note_count: int = Field(
        default=0,
        description='Number of notes in this vault.',
    )

    last_note_added_at: dt.datetime | None = Field(
        default=None,
        description='Timestamp of the most recently added note in this vault.',
    )

    access: list[str] | None = Field(
        default=None,
        description=(
            'Effective permissions for the current API key on this vault. '
            'None when auth is disabled. Example: ["read", "write"].'
        ),
    )


class DefaultVaultsResponse(BaseModel):
    """Response model for the default vaults endpoint."""

    active_vault: VaultDTO = Field(
        description='The active (writer) vault.',
    )
    reader_vaults: list[VaultDTO] = Field(
        default_factory=list,
        description='Default reader vaults for search/retrieval.',
    )


class NoteCreateDTO(BaseModel):
    """DTO for creating a Note artifact (input/ingestion)."""

    name: str = Field(
        description='The name of the note.',
        examples=['Meeting Notes'],
    )
    note_key: str | None = Field(
        default=None,
        description='A unique stable key for the note to enable incremental updates.',
        examples=['my-stable-note-id'],
    )
    description: str = Field(
        description='A brief description or summary of the note.',
        examples=['Notes from the weekly team sync.'],
    )
    content: Base64Bytes = Field(
        description='Base64 encoded content of the note.',
        examples=['VGhpcyBpcyBhIHRlc3Qgbm90ZS4='],
    )
    files: dict[str, Base64Bytes] = Field(
        default_factory=dict,
        description='Dictionary mapping filenames to Base64 encoded content.',
        examples=[
            {
                'image.png': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
            }
        ],
    )
    tags: list[str] = Field(
        default_factory=list,
        description='List of tags associated with the note.',
        examples=[['meeting', 'work']],
    )
    vault_id: str | UUID | None = Field(
        default=None,
        description='Optional target vault ID or name.',
    )
    user_notes: str | None = Field(
        default=None,
        description='Optional user-provided context or commentary to include in the note.',
    )
    author: str | None = Field(
        default=None,
        description='Author of the note.',
        examples=['alice'],
    )
    template: str | None = Field(
        default=None,
        description='Template slug used to create this note (e.g. "general_note").',
        examples=['general_note', 'technical_brief'],
    )
    filename: str | None = Field(
        default=None,
        description='Original filename of the content (e.g. "report.pdf"). '
        'When present and the extension is not .md, the server converts '
        'the content to Markdown before ingestion using FileContentProcessor.',
        examples=['report.pdf', 'slides.pptx', 'meeting-notes.md'],
    )
    intent_class: IntentClass | None = Field(
        default=None,
        description=(
            'Optional intent override applied to all facts extracted from the note. '
            'Bypasses the write-time classifier when set. Use "ephemeral" for transient '
            'context, "durable" for default-lived facts, "permanent" for enduring '
            'preferences/conventions.'
        ),
    )
    risk_class: RiskClass | None = Field(
        default=None,
        description=(
            'Optional risk override applied to all facts extracted from the note. '
            'Bypasses the write-time classifier. Set "safety" to refuse persistence; '
            '"sensitive"/"private" for restricted handling; "none" for default.'
        ),
    )

    @property
    def content_decoded(self) -> bytes:
        """Return the decoded raw bytes of the note content."""
        return base64.b64decode(self.content)

    @property
    def files_decoded(self) -> dict[str, bytes]:
        """Return a dictionary of filenames to decoded raw bytes."""
        return {k: base64.b64decode(v) for k, v in self.files.items()}

    @field_serializer('content')
    def serialize_content(self, content: bytes) -> str:
        try:
            return content.decode('utf-8')
        except UnicodeDecodeError:
            return base64.b64encode(content).decode('ascii')

    @field_serializer('files')
    def serialize_files(self, files: dict[str, bytes]) -> dict[str, str]:
        result: dict[str, str] = {}
        for k, v in files.items():
            try:
                result[k] = v.decode('utf-8')
            except UnicodeDecodeError:
                result[k] = base64.b64encode(v).decode('ascii')
        return result


class OverlappingNote(BaseModel):
    """A note that overlaps with the newly ingested one."""

    note_id: UUID
    similarity: float
    title: str | None = None


class IngestResponse(BaseModel):
    """Response from ingestion."""

    status: str = Field(
        description='Status of the ingestion operation.',
        examples=['success', 'skipped', 'failed'],
    )

    note_id: str | None = Field(
        default=None,
        description='The unique identifier of the ingested note.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )

    unit_ids: list[UUID] = Field(
        default_factory=list,
        description='List of UUIDs for the extracted memory units.',
        examples=[['123e4567-e89b-12d3-a456-426614174001', '123e4567-e89b-12d3-a456-426614174002']],
    )

    reason: str | None = Field(
        default=None,
        description='Reason for skipping or failure, if applicable.',
        examples=['idempotency_check'],
    )

    overlapping_notes: list[OverlappingNote] = Field(
        default_factory=list,
        description='Notes with high similarity to the ingested content (>0.85).',
    )


class WebhookPayload(BaseModel):
    """Payload accepted by the webhook ingestion endpoint."""

    title: str = Field(
        description='Title of the note to ingest.',
        examples=['Daily standup notes'],
    )
    content: str = Field(
        description='Plain-text or Markdown content of the note.',
        examples=['## Summary\nWe discussed the roadmap.'],
    )
    source: str = Field(
        description='Identifier for the sending system (used for idempotent note_key generation).',
        examples=['slack-bot', 'github-webhook', 'zapier'],
    )
    description: str | None = Field(
        default=None,
        description='Optional description or summary of the note.',
    )
    tags: list[str] = Field(
        default_factory=list,
        description='Optional tags to associate with the note.',
        examples=[['meeting', 'standup']],
    )
    vault_id: str | UUID | None = Field(
        default=None,
        description='Optional target vault ID or name.',
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description='Arbitrary metadata from the webhook source.',
    )


class ReflectionResultDTO(BaseModel):
    """DTO for reflection results."""

    entity_id: UUID = Field(
        description='The UUID of the entity reflected upon.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )

    new_observations: list[ObservationDTO] = Field(
        description='List of new observations generated during reflection.',
    )

    status: str = Field(
        default='success',
        description='Status of the reflection operation.',
        examples=['success', 'failed'],
    )


class ReflectionQueueDTO(BaseModel):
    """DTO for reflection queue item."""

    entity_id: UUID = Field(
        description='The UUID of the entity in the queue.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )
    vault_id: UUID = Field(
        description='The UUID of the vault associated with the entity.',
        examples=['123e4567-e89b-12d3-a456-426614174000'],
    )
    priority_score: float = Field(
        default=0.0,
        description='Priority score for processing.',
        examples=[0.85],
    )


class DeadLetterItemDTO(BaseModel):
    """DTO for a dead-lettered reflection queue item."""

    id: UUID = Field(description='Queue item ID.')
    entity_id: UUID = Field(description='Entity that failed reflection.')
    vault_id: UUID = Field(description='Vault of the entity.')
    priority_score: float = Field(default=0.0, description='Priority score.')
    retry_count: int = Field(default=0, description='Number of retries attempted.')
    max_retries: int = Field(default=3, description='Maximum retries before dead letter.')
    last_error: str | None = Field(default=None, description='Last error message.')
    status: str = Field(default='dead_letter', description='Current status.')


class BatchIngestRequest(BaseModel):
    """Request for batch ingestion of notes."""

    notes: list[NoteCreateDTO] = Field(..., description='List of notes to ingest.')
    vault_id: UUID | str | None = Field(
        default=None,
        description='Optional target vault ID or name that overrides note-level vault.',
    )
    batch_size: int = Field(default=32, description='Internal processing chunk size.', ge=1, le=100)


class BatchIngestResponse(BaseModel):
    """Final response from a batch ingestion operation."""

    processed_count: int = Field(default=0, description='Number of successfully processed notes.')
    skipped_count: int = Field(default=0, description='Number of skipped notes (e.g. duplicates).')
    failed_count: int = Field(default=0, description='Number of notes that failed to process.')
    note_ids: list[str] = Field(default_factory=list, description='List of created Note UUIDs.')
    errors: list[dict[str, Any]] = Field(
        default_factory=list, description='Detailed error information indexed by input.'
    )


class BatchJobStatus(BaseModel):
    """Current status of an asynchronous batch job."""

    job_id: UUID = Field(..., description='The unique identifier of the batch job.')
    status: str = Field(
        ...,
        description='Current status of the job.',
        examples=['pending', 'processing', 'completed', 'failed'],
    )
    progress: str | None = Field(default=None, description='Optional human-readable progress info.')
    processed_count: int | None = Field(
        default=None, description='Notes processed so far (including skipped/failed).'
    )
    total_count: int | None = Field(default=None, description='Total notes in the batch.')
    result: BatchIngestResponse | None = Field(
        default=None, description='Final results, available when status is "completed".'
    )


class SystemStatsCountsDTO(BaseModel):
    """Counts for system entities."""

    notes: int = Field(default=0, description='Total number of notes (documents).')
    memories: int = Field(description='Total number of memory units.')
    entities: int = Field(description='Total number of entities.')
    reflection_queue: int = Field(description='Number of items in the reflection queue.')


class NodeDTO(BaseModel):
    """DTO for a note node (section produced by PageIndex)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    note_id: UUID
    vault_id: UUID
    node_hash: str | None = None
    title: str
    text: str
    level: int
    seq: int
    status: str
    created_at: dt.datetime


class NoteDTO(BaseModel):
    """DTO for a Note (output/retrieval)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None = None
    name: str | None = None
    original_text: str | None = None
    created_at: dt.datetime
    publish_date: dt.datetime | None = None
    vault_id: UUID
    vault_name: str | None = None
    description: str | None = None
    assets: list[str] = Field(default_factory=list)
    doc_metadata: dict[str, Any] = Field(default_factory=dict)
    template: str | None = None


class BlockSummaryDTO(BaseModel):
    """Block-level summary from extraction."""

    topic: str
    key_points: list[str] = Field(default_factory=list)


class NoteListItemDTO(BaseModel):
    """Lightweight DTO for note listing — summaries instead of full text."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None = None
    name: str | None = None
    created_at: dt.datetime
    publish_date: dt.datetime | None = None
    vault_id: UUID
    vault_name: str | None = None
    description: str | None = None
    assets: list[str] = Field(default_factory=list)
    doc_metadata: dict[str, Any] = Field(default_factory=dict)
    template: str | None = None
    summaries: list[BlockSummaryDTO] = Field(default_factory=list)


class MemoryLinkDTO(BaseModel):
    """A link between memory units, surfaced in search results."""

    unit_id: UUID
    note_id: UUID | None = None
    note_title: str | None = None
    relation: str
    weight: float = 1.0
    time: dt.datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelatedNoteDTO(BaseModel):
    """A note related via shared entities."""

    note_id: UUID
    title: str | None = None
    shared_entities: list[str] = Field(default_factory=list)
    strength: float = 0.0


class UnitHistoryNodeDTO(BaseModel):
    """A node in a memory unit's contradiction-graph timeline.

    The tree is rooted at the queried unit (depth=0); each predecessor
    represents an older unit that the current node supersedes via a
    ``contradicts`` or ``weakens`` link. Walked backward in time.
    """

    unit_id: UUID = Field(description='Memory unit at this depth.')
    text: str = Field(description='Memory unit text content.')
    note_id: UUID | None = Field(
        default=None,
        description='Source note ID (null when the unit has no source note).',
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description='Current confidence on the unit (0.0-1.0).',
    )
    event_date: dt.datetime | None = Field(
        default=None,
        description='When the unit was originally observed (event_date / mentioned_at fallback).',
    )
    link_type: str | None = Field(
        default=None,
        description=(
            'Type of supersession edge from this node to its parent (the newer '
            "unit that supersedes it): 'contradicts' or 'weakens'. None for the "
            'starting unit (root).'
        ),
    )
    link_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description='Metadata on the link (e.g., reasoning, temporal_basis).',
    )
    depth: int = Field(
        description='0 for the starting unit, 1 for direct predecessors, etc.',
    )
    predecessors: list['UnitHistoryNodeDTO'] = Field(
        default_factory=list,
        description='Older units this one supersedes (branching for multi-predecessor cases).',
    )
    truncated: bool = Field(
        default=False,
        description=(
            'True when further predecessors exist but were not expanded — '
            'either because ``max_depth`` was reached or the node was already '
            'visited via another path.'
        ),
    )


class NoteSearchResult(BaseModel):
    """Result of a note search."""

    note_id: UUID
    metadata: dict[str, Any]
    summaries: list[BlockSummaryDTO] = Field(default_factory=list)
    score: float = 0.0
    vault_id: UUID | None = None
    vault_name: str | None = None
    reasoning: list[dict[str, Any]] | None = Field(
        default=None,
        description='Identified sections with reasoning text and node IDs (populated when reason=True).',
    )
    answer: str | None = Field(
        default=None,
        description='LLM-generated answer when summarize=True.',
    )
    note_status: str | None = Field(
        default=None,
        description='Derived status: active, partially_superseded, superseded.',
    )
    related_notes: list[RelatedNoteDTO] = Field(default_factory=list)
    links: list[MemoryLinkDTO] = Field(default_factory=list)


class NoteSearchRequest(BaseModel):
    """Request to search for notes."""

    query: str
    limit: int = 10
    vault_ids: list[UUID | str] | None = None
    expand_query: bool = False
    fusion_strategy: str = 'rrf'
    strategies: list[str] = Field(default=['semantic', 'keyword', 'graph', 'temporal'])
    strategy_weights: dict[str, float] | None = Field(default=None)
    after: dt.datetime | None = Field(
        default=None, description='Only return notes created after this date (ISO 8601).'
    )
    before: dt.datetime | None = Field(
        default=None, description='Only return notes created before this date (ISO 8601).'
    )
    tags: list[str] | None = Field(
        default=None, description='Only return notes with ALL of these tags.'
    )
    reference_date: dt.datetime | None = Field(
        default=None,
        description=(
            'Reference point for resolving relative temporal expressions '
            '(e.g. "last week"). Defaults to now (UTC) when None.'
        ),
    )
    reason: bool = Field(
        default=False,
        description='Run skeleton-tree identification — returns reasoning + relevant section IDs.',
    )
    summarize: bool = Field(
        default=False,
        description='Synthesize a full answer from identified sections (implies reason=True).',
    )
    rerank: bool = Field(
        default=True,
        description='Apply cross-encoder reranking when a reranker is available.',
    )
    mmr_lambda: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            'MMR lambda for relevance-diversity trade-off. '
            '1.0 = pure relevance, 0.0 = max diversity. '
            'None = use server config default (which itself defaults to 0.8).'
        ),
    )

    @model_validator(mode='after')
    def _ensure_reason_if_summarize(self) -> 'NoteSearchRequest':
        if self.summarize:
            self.reason = True
        return self


class SummaryRequest(BaseModel):
    """Request for AI-generated summary of search results."""

    query: str = Field(..., description='The original search query.')
    texts: list[str] = Field(
        ...,
        description='Search result texts to summarize.',
        max_length=50,
    )


class SummaryResponse(BaseModel):
    """Response containing an AI-generated summary with citations."""

    summary: str = Field(
        description='AI-generated summary with bracket citations (e.g. [0], [1]).',
    )


class VaultSummaryDTO(BaseModel):
    """DTO for a vault-level summary."""

    id: UUID = Field(description='Unique identifier for the vault summary.')
    vault_id: UUID = Field(description='The vault this summary describes.')
    narrative: str = Field(description='Short thematic synthesis of vault contents.')
    themes: list[dict[str, Any]] = Field(
        description='Extracted themes: [{name, description, note_count, trend, '
        'last_addition, representative_titles}].'
    )
    inventory: dict[str, Any] = Field(
        description='Computed content stats: total_notes, total_entities, date_range, '
        'by_template, by_source_domain, top_tags, recent_activity, '
        'last_activity_at (ISO 8601 UTC string or null), '
        'days_since_last_note (int or null). The time-sensitive fields '
        '(recent_activity, last_activity_at, days_since_last_note, '
        'date_range.latest) are recomputed against wall-clock now on every read.'
    )
    key_entities: list[dict[str, Any]] = Field(
        description='Top entities by mention count: [{name, type, mention_count}].'
    )
    version: int = Field(description='Summary version number.')
    notes_incorporated: int = Field(description='Number of notes incorporated.')
    created_at: dt.datetime = Field(description='When the summary was created.')
    updated_at: dt.datetime = Field(description='When the summary was last updated.')


class PageMetadataDTO(BaseModel):
    """Metadata from a note's page index."""

    title: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    publish_date: str | None = None
    source_uri: str | None = None
    has_assets: bool = False
    vault_id: UUID | None = None
    vault_name: str | None = None
    total_tokens: int | None = None


class SectionSummaryDTO(BaseModel):
    """5W summary of a document section."""

    who: str | None = None
    what: str | None = None
    how: str | None = None
    when: str | None = None
    where: str | None = None


class TOCNodeDTO(BaseModel):
    """A node in the page index table-of-contents."""

    id: str
    title: str
    level: int
    summary: SectionSummaryDTO | None = None
    token_estimate: int | None = None
    subtree_tokens: int | None = None
    children: list['TOCNodeDTO'] = Field(default_factory=list)


class PageIndexDTO(BaseModel):
    """Full page index with metadata and TOC."""

    metadata: PageMetadataDTO
    toc: list[TOCNodeDTO]
    total_tokens: int | None = None


class KVEntryDTO(BaseModel):
    """DTO for a key-value store entry."""

    id: UUID
    key: str
    value: str
    expires_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class KVProcedureValueDTO(BaseModel):
    """Procedure-key value envelope returned when ``include_history=True``."""

    value: str
    version: int
    history: list[dict[str, Any]]


class KVProcedureEntryDTO(BaseModel):
    """DTO for a ``procedure:`` KV entry with full envelope visible.

    Returned by the kv_get endpoint when ``include_history=true``. The
    ``value`` field is the structured envelope (active value + version +
    capped history). For non-procedure keys callers should use
    :class:`KVEntryDTO` (default endpoint shape).
    """

    id: UUID
    key: str
    value: KVProcedureValueDTO
    expires_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class ProcedureOutcomeDTO(BaseModel):
    """One row of the procedure-observations briefing surface.

    Returned by ``MemexAPI.list_top_procedure_outcomes`` and the
    ``GET /api/v1/kv/procedure-observations`` endpoint. Each row exposes
    the procedure KV key, success/failure MW counters, the
    Beta-Bernoulli posterior-mean Memory Worth score
    (``memex_core.services.outcomes.compute_mw_score``), and the
    ``last_outcome_at`` timestamp.
    """

    kv_key: str
    success_co_count: int
    failure_co_count: int
    mw_score: float
    last_outcome_at: dt.datetime | None = None


class KVPutRequest(BaseModel):
    """Request to create or update a key-value entry."""

    key: str
    value: str
    embedding: list[float] | None = None
    ttl_seconds: int | None = None


class KVSearchRequest(BaseModel):
    """Request to semantically search key-value entries."""

    query: str
    namespaces: list[str] | None = None
    limit: int = Field(5, ge=1, le=500)


class FindNoteResult(BaseModel):
    """Result from a fuzzy title search over notes."""

    note_id: UUID
    title: str
    score: float
    vault_id: UUID
    created_at: dt.datetime
    publish_date: dt.datetime | None = None
    status: str


# ---------------------------------------------------------------------------
# Survey (broad topic exploration)
# ---------------------------------------------------------------------------


class SurveyRequest(BaseModel):
    """Request for broad topic survey — decomposes into sub-questions and aggregates."""

    query: str = Field(..., description='Broad topic or panoramic query to survey.')
    vault_ids: list[UUID | str] | None = Field(
        default=None,
        description='Vault UUIDs or names to search. If None, uses default reader vault.',
    )
    limit_per_query: int = Field(
        default=10, ge=1, le=50, description='Max results per sub-question.'
    )
    token_budget: int | None = Field(
        default=None,
        description='Max token budget for all results. Greedy packing: truncates when exceeded.',
    )


class SurveyFact(BaseModel):
    """A single fact within a survey topic."""

    id: UUID = Field(description='Memory unit ID.')
    text: str = Field(description='The fact text.')
    fact_type: str = Field(description='Type: world, event, or observation.')
    score: float | None = Field(default=None, description='Relevance score.')


class SurveyTopic(BaseModel):
    """A group of facts from a single source note."""

    note_id: UUID = Field(description='Source note ID.')
    title: str | None = Field(default=None, description='Note title.')
    fact_count: int = Field(description='Number of facts from this note.')
    facts: list[SurveyFact] = Field(default_factory=list, description='Facts from this note.')


class SurveyResponse(BaseModel):
    """Response from a broad topic survey."""

    query: str = Field(description='The original survey query.')
    sub_queries: list[str] = Field(description='Decomposed sub-questions.')
    topics: list[SurveyTopic] = Field(
        default_factory=list, description='Results grouped by source note.'
    )
    total_notes: int = Field(default=0, description='Total unique notes found.')
    total_facts: int = Field(default=0, description='Total unique facts found.')
    truncated: bool = Field(
        default=False, description='True if results were truncated by token budget.'
    )


# ---------------------------------------------------------------------------
# TOC filtering utility
# ---------------------------------------------------------------------------


def filter_toc(
    toc: list[dict[str, Any]],
    depth: int | None = None,
    parent_node_id: str | None = None,
) -> list[dict[str, Any]]:
    """Filter a TOC tree by depth and/or parent node.

    Pure function — no I/O.  Shared by core (NoteService) and MCP server so
    that both layers can apply the same filtering without MCP importing core
    internals.
    """
    if parent_node_id is not None:

        def _find_subtree(
            nodes: list[dict[str, Any]], target_id: str
        ) -> list[dict[str, Any]] | None:
            for node in nodes:
                if node.get('id') == target_id:
                    return node.get('children', [])
                found = _find_subtree(node.get('children', []), target_id)
                if found is not None:
                    return found
            return None

        subtree = _find_subtree(toc, parent_node_id)
        if subtree is None:
            return []
        toc = subtree

    if depth is not None and depth >= 0:
        # depth=0 -> roots + direct children (H1 + H2 overview)
        # depth=1 -> full tree (no trimming)
        # depth=N (N>=1) -> full tree
        effective_depth = depth + 1

        def _trim_depth(nodes: list[dict[str, Any]], current: int) -> list[dict[str, Any]]:
            if current > effective_depth:
                return []
            result = []
            for node in nodes:
                trimmed = dict(node)
                trimmed['children'] = _trim_depth(node.get('children', []), current + 1)
                result.append(trimmed)
            return result

        if depth == 0:
            toc = _trim_depth(toc, 0)
        # depth >= 1: return full tree (no trimming needed)

    return toc


_APPEND_JOINERS: dict[str, str] = {
    'paragraph': '\n\n',
    'newline': '\n',
    'none': '',
}

# Matches anything that the markdown frontmatter parser would treat as the
# start of a YAML frontmatter block — i.e. ``---`` followed by optional
# whitespace and a newline. Mirrors ``FRONTMATTER_PATTERN`` in
# ``memex_core.services.ingestion`` so the schema validator and the service
# agree on what "starts with frontmatter" means.
APPEND_FRONTMATTER_PREFIX_PATTERN = re.compile(r'\A---[ \t]*\r?\n')

# Server-side cap on the delta payload, in BYTES (not characters). 200 KiB.
# Pydantic's ``max_length`` counts characters, so we enforce the byte cap in
# the model_validator and document it in the user-facing description.
APPEND_DELTA_MAX_BYTES = 200_000


def append_joiner_separator(joiner: str) -> str:
    """Map a joiner enum value to its actual separator string."""
    try:
        return _APPEND_JOINERS[joiner]
    except KeyError as exc:
        raise ValueError(
            f'Unknown joiner {joiner!r}; expected one of {sorted(_APPEND_JOINERS)}'
        ) from exc


def validate_append_delta(delta: str) -> None:
    """Raise DeltaValidationError if ``delta`` violates any shape rule of the append endpoint."""
    from memex_common.exceptions import DeltaValidationError

    if not delta:
        raise DeltaValidationError('delta must not be empty.')
    if APPEND_FRONTMATTER_PREFIX_PATTERN.match(delta):
        raise DeltaValidationError(
            "delta must not begin with '---' followed by a newline "
            '(would be ambiguous with frontmatter).'
        )
    if not delta.strip():
        raise DeltaValidationError('delta must contain non-whitespace characters.')
    if '\x00' in delta:
        raise DeltaValidationError('delta must not contain NUL bytes (\\x00).')
    if len(delta.encode('utf-8')) > APPEND_DELTA_MAX_BYTES:
        raise DeltaValidationError(f'delta exceeds {APPEND_DELTA_MAX_BYTES} UTF-8 bytes.')


class NoteAppendRequest(BaseModel):
    """Request body for POST /api/v1/notes/append.

    The caller identifies the target note by note_key + vault_id (the dominant
    pattern — agents created the note with their own stable key and never see
    a UUID), or by note_id (convenience for callers that already hold one
    from a prior search). Exactly one identifier is required; passing both
    is rejected with 422 to surface caller confusion loudly.

    `delta` is the new content snippet to append. It is concatenated onto the
    end of the parent's body using `joiner` and re-ingested through the
    existing incremental block-diff pipeline so only the new chunks invoke
    the LLM.

    `append_id` is a caller-supplied UUID. Retrying with the same append_id
    replays the cached outcome from the note_appends audit table without
    mutating the body twice. Reusing an append_id with a different parent or
    a different delta raises 409.
    """

    note_key: str | None = Field(
        default=None,
        description=(
            'Stable user-facing key the note was created with. Preferred '
            'identifier for agents that own the key.'
        ),
        examples=['session-2026-04-26-jasper'],
    )
    vault_id: str | UUID | None = Field(
        default=None,
        description='Vault scope. Required when identifying by note_key.',
    )
    note_id: UUID | None = Field(
        default=None,
        description=(
            'Direct UUID. Convenience for callers that already have one. '
            'Mutually exclusive with note_key — passing both returns 422.'
        ),
    )
    delta: str = Field(
        description=(
            'New content to append. Must NOT begin with `---` followed by a '
            'newline (would be ambiguous with frontmatter), must not be '
            'whitespace-only, and must fit within 200_000 UTF-8 bytes. NUL '
            'bytes (\\x00) are rejected — Postgres TEXT cannot store them.'
        ),
    )
    append_id: UUID = Field(
        description=(
            'Caller-supplied idempotency token. Reusing the same value '
            'with the same delta+parent is a safe replay.'
        ),
    )
    joiner: str = Field(
        default='paragraph',
        description=(
            'Separator between parent body and delta. '
            "'paragraph' (\\n\\n, default), 'newline' (\\n), or 'none' (no separator)."
        ),
    )
    user_notes: str | None = Field(
        default=None,
        description=(
            'Optional user-provided commentary. Stored on the note metadata; '
            'NOT re-injected into the body.'
        ),
    )

    @model_validator(mode='after')
    def _require_one_identifier(self) -> 'NoteAppendRequest':
        if self.note_id is None and self.note_key is None:
            raise ValueError('One of note_id or note_key is required.')
        if self.note_id is not None and self.note_key is not None:
            raise ValueError(
                'Pass either note_id or note_key, not both. If you meant note_key, drop note_id.'
            )
        if self.note_id is None and self.note_key is not None and self.vault_id is None:
            raise ValueError('vault_id is required when identifying by note_key.')
        if self.joiner not in _APPEND_JOINERS:
            raise ValueError(
                f'Unknown joiner {self.joiner!r}; expected one of {sorted(_APPEND_JOINERS)}.'
            )
        validate_append_delta(self.delta)
        return self


class NoteAppendResponse(BaseModel):
    """Response body for POST /api/v1/notes/append."""

    status: str = Field(
        description="'success' on first apply, 'replayed' on idempotent retry.",
    )
    note_id: UUID = Field(description='The (unchanged) note_id of the appended-to note.')
    append_id: UUID = Field(description='Echoes the caller-supplied idempotency token.')
    content_hash: str = Field(description='content_hash of the resulting full body.')
    delta_bytes: int = Field(description='Length of the delta in UTF-8 bytes.')
    new_unit_ids: list[UUID] = Field(
        default_factory=list,
        description='Memory units newly extracted from the delta.',
    )


class DueUnitDTO(BaseModel):
    """Due-for-review wire DTO.

    Mirrors the in-process ``memex_core.services.revisitation.DueUnit``
    dataclass. Lives in ``memex_common`` so the remote ``MemexClient`` can
    return the same shape without depending on ``memex_core``.
    """

    unit_id: UUID
    text_preview: str
    revisit_due_at: dt.datetime
    intent_class: str
