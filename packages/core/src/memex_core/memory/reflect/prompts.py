"""
DSPy prompts and signatures for the Hindsight Reflect Loop.
"""

from typing import Literal
from uuid import UUID

import dspy
from pydantic import BaseModel, Field, model_validator

# Shared strict description for index-based citations
EVIDENCE_INDEX_DESCRIPTION = (
    'The zero-based integer index (or indices) of the source items in the provided context. '
    'Example: 0 for the first item, 1 for the second. '
    'STRICT RULE: Only use Null/None (or an empty list) if the provided context '
    'contains NO specific evidence to support the statement. If evidence is '
    'present in the context, you MUST provide the corresponding integer index.'
)


class HasEvidenceIndices(BaseModel):
    """Mixin for models that reference source evidence by index."""

    evidence_indices: list[int] = Field(
        default_factory=list, description=EVIDENCE_INDEX_DESCRIPTION
    )


class CandidateObservation(HasEvidenceIndices):
    """A candidate observation generated during the seed phase."""

    content: str = Field(
        description='The proposed observation content describing a pattern or trait.'
    )
    reasoning: str | None = Field(
        default=None,
        description='Explanation of why this observation was generated. DO NOT refer to evidence by index/number.',
    )


# =============================================================================
# SHARED CONTEXT MODELS
# =============================================================================


class ReflectMemoryContext(BaseModel):
    """Reduced memory representation for LLM context."""

    index_id: int = Field(description='The integer reference ID for this memory (0, 1, ...).')
    content: str = Field(description='The core fact text.')
    occurred: str = Field(description='ISO timestamp or relative time.')


class ReflectObservationContext(BaseModel):
    """Reduced observation representation for LLM context."""

    index_id: int = Field(description='The integer reference ID for this observation.')
    title: str = Field(description='Observation title.')
    content: str = Field(description='Observation content.')


# =============================================================================
# PHASE 1: SEED
# =============================================================================


class SeedPhaseSignature(dspy.Signature):
    """
    Analyze a set of memories and generate high-level observations (mental models) about the entity or topic.
    Focus on patterns, preferences, behavioral traits, and recurring themes.
    Skip observations that are already covered by the 'existing_observations'.

    STRICT RULE: All observations MUST be written in English, regardless of the language of the source memories.
    """

    memories_context: list[ReflectMemoryContext] = dspy.InputField(
        desc='List of raw memories to analyze'
    )
    topic: str = dspy.InputField(desc='The specific topic or entity to focus on')
    existing_observations: list[ReflectObservationContext] = dspy.InputField(
        desc='List of observations we already know (do not repeat these)'
    )

    candidates: list[CandidateObservation] = dspy.OutputField(
        desc='List of NEW candidate observations found. MUST be in English.'
    )


# =============================================================================
# PHASE 0: UPDATE EXISTING
# =============================================================================


class NewEvidenceItem(BaseModel):
    """A new evidence item found for an observation."""

    memory_id: int | str | None = Field(description=EVIDENCE_INDEX_DESCRIPTION)
    quote: str = Field(
        description='The exact text quote from the memory that supports the observation.'
    )
    relevance_explanation: str = Field(
        description='Explanation of why this quote is relevant/supportive. DO NOT refer to indices.'
    )
    timestamp: str = Field(description='ISO timestamp of when the memory/event occurred.')


class UpdatedObservationResult(BaseModel):
    """Result for updating a single observation."""

    observation_index: int = Field(description='Index of the observation in the provided list.')
    new_evidence: list[NewEvidenceItem] = Field(
        description='List of new supporting evidence items found.'
    )
    has_contradiction: bool = Field(
        description='True if strongly contradictory evidence was found.'
    )
    contradiction_note: str | None = Field(
        default=None, description='Explanation of the contradiction if one exists.'
    )


class UpdateExistingSignature(dspy.Signature):
    """
    For each existing observation, check the provided potential evidence.
    Extract EXACT quotes that support the observation.
    Flag if any evidence strictly contradicts the observation.
    """

    recent_memories: list[ReflectMemoryContext] = dspy.InputField(
        desc='New memories that might support or contradict existing observations'
    )
    existing_observations: list[ReflectObservationContext] = dspy.InputField(
        desc='Existing observations to check against new memories'
    )

    updates: list[UpdatedObservationResult] = dspy.OutputField(desc='Updates for each observation')


# =============================================================================
# PHASE 3: VALIDATE
# =============================================================================


class ValidatedObservation(BaseModel):
    id: UUID | None = Field(
        default=None,
        description=(
            'Stable identifier for this observation. LLMs do not mint UUIDs; '
            'this field is populated by the reconstruction layer from the provenance '
            'mapping (existing UUID for merged/kept observations, fresh uuid4 for added).'
        ),
    )
    title: str = Field(description='Concise title for the observation.')
    content: str = Field(description='Detailed content of the observation.')
    evidence: list[NewEvidenceItem] = Field(description='List of verified supporting evidence.')


class UnvalidatedCandidateObservation(BaseModel):
    content: str = Field(
        description='The proposed observation content describing a pattern or trait.'
    )
    context: list[ReflectMemoryContext] = Field(
        description='List of retrieved supporting/contradicting evidence for this candidate.'
    )


class ValidatePhaseSignature(dspy.Signature):
    """
    Validate candidate observations against retrieved evidence.
    Reject candidates that are hallucinations or lack strong evidence.
    For accepted candidates, extract EXACT quotes from the supporting memories.

    STRICT RULE: All titles and content MUST be written in English.
    """

    candidates: list[UnvalidatedCandidateObservation] = dspy.InputField(
        desc='List of candidate observations with their supporting/contradicting evidence'
    )

    validated_observations: list[ValidatedObservation] = dspy.OutputField(
        desc='List of fully validated observations. MUST be in English.'
    )


# =============================================================================
# PHASE 4: COMPARE
# =============================================================================


class ObservationProvenance(BaseModel):
    """Per-output provenance entry telling the reconstruction layer how to pick a stable UUID.

    The LLM emits one entry per item in ``ComparePhaseOutput.observations`` (same index).
    """

    output_index: int = Field(
        description='Index into ComparePhaseOutput.observations this provenance entry describes.',
    )
    status: Literal['added', 'merged', 'kept'] = Field(
        description=(
            "'added' for a genuinely new observation (no existing predecessors); "
            "'merged' when this output combines two or more existing observations; "
            "'kept' when this output reuses a single existing observation unchanged."
        ),
    )
    merged_from_existing_indices: list[int] = Field(
        default_factory=list,
        description=(
            "0-based indices into the 'existing_context' input that this output corresponds to. "
            "Empty for status='added'. One index for 'kept'. Two or more for 'merged'. "
            'Example: if output index 0 combines existing #1 and existing #2, emit '
            "{output_index: 0, status: 'merged', merged_from_existing_indices: [1, 2]}."
        ),
    )


class ComparePhaseOutput(BaseModel):
    observations: list[ValidatedObservation] = Field(
        description='The final merged list of observations.'
    )
    provenance: list[ObservationProvenance] = Field(
        default_factory=list,
        description=(
            'Per-output provenance entry (same length as observations). Tells the '
            'reconstruction layer how to assign stable UUIDs: reuse the existing UUID '
            "for 'merged' (lowest index wins) or 'kept', mint fresh for 'added'."
        ),
    )
    entity_summary: str = Field(
        description='One-sentence summary of this entity based on all observations. English only.'
    )


class ReflectEvidenceContext(BaseModel):
    """Reduced evidence representation for LLM context."""

    index_id: int = Field(description='The integer reference ID for this evidence.')
    quote: str = Field(description='The exact text quote.')
    occurred: str = Field(description='ISO timestamp or relative time.')


class ReflectComparisonObservation(HasEvidenceIndices):
    """Observation representation with evidence references for comparison."""

    index_id: int = Field(description='The integer reference ID for this observation.')
    title: str = Field(description='Observation title.')
    content: str = Field(description='Observation content.')


class ComparePhaseSignature(dspy.Signature):
    """
    Merge 'New Observations' into 'Existing Observations'.
    - If a new observation replicates an existing one, merge them (combine evidence).
    - If a new observation conflicts with an existing one, decide which is more supported or flag the conflict.
    - If a new observation is unique, add it.
    - Also produce a one-sentence summary of the entity ('entity_summary').

    The 'evidence_context' list contains all unique facts/evidence referenced by the observations.
    Observations refer to these facts by their 0-based index in 'evidence_context'.

    STRICT RULE: All output observations MUST be in English.
    """

    entity_name: str = dspy.InputField(desc='The name of the entity being summarized.')
    evidence_context: list[ReflectEvidenceContext] = dspy.InputField(
        desc='List of unique evidence facts.'
    )
    existing_context: list[ReflectComparisonObservation] = dspy.InputField(
        desc='Current list of observations (referencing evidence indices)'
    )
    new_context: list[ReflectComparisonObservation] = dspy.InputField(
        desc='New validated observations to merge in (referencing evidence indices)'
    )

    result: ComparePhaseOutput = dspy.OutputField(
        desc='Final merged list of observations. MUST be in English.'
    )


# =============================================================================
# PHASE 6: ENRICH (Memory Evolution)
# =============================================================================


class EnrichedTagSet(BaseModel):
    """Enriched tags and keywords generated for a single memory unit."""

    memory_index: int = Field(
        description='The zero-based index of the memory in the provided list.'
    )
    enriched_tags: list[str] = Field(
        description=(
            'New conceptual tags that the memory is now understood to relate to, '
            'based on the entity summary and observations. Lowercase, 1-3 words each, English only. '
            'Do NOT duplicate tags already present in the memory text or existing tags.'
        ),
    )
    enriched_keywords: list[str] = Field(
        description=(
            'New search keywords derived from the mental model that would help find this memory. '
            'Lowercase, single words or short phrases, English only.'
        ),
    )


class EnrichmentSignature(dspy.Signature):
    """
    Given a mental model (entity summary + observations) and the memory units that served as
    evidence, generate enriched tags and keywords for each memory unit.

    The goal is to make memories discoverable for concepts that were NOT apparent at extraction
    time but are now understood through reflection. For example, a memory about "rewriting auth
    middleware" should gain tags like "compliance" if the mental model reveals the rewrite is
    compliance-driven.

    Rules:
    - Tags MUST be lowercase, 1-3 words, English only.
    - Do NOT duplicate tags that already appear in the memory text or existing tags shown in brackets.
    - Only generate tags that are genuinely implied by the mental model's understanding.
    - It is acceptable to return an empty list for memories that don't need enrichment.
    """

    entity_name: str = dspy.InputField(desc='The name of the entity whose mental model was built.')
    entity_summary: str = dspy.InputField(desc='One-sentence summary of the entity.')
    observations: list[ReflectObservationContext] = dspy.InputField(
        desc='The observations (mental model insights) synthesized during reflection.'
    )
    memories: list[ReflectMemoryContext] = dspy.InputField(
        desc='The memory units that contributed as evidence. Tags in [brackets] are already assigned.'
    )

    enrichments: list[EnrichedTagSet] = dspy.OutputField(
        desc='Enriched tag sets for each memory that warrants new tags. May be shorter than input.'
    )


# =============================================================================
# REFRESH: surgical re-synthesis of a single observation on surviving evidence
# =============================================================================


class RefreshedObservation(BaseModel):
    """Result of re-synthesizing an observation on a strictly-smaller evidence set."""

    content: str = Field(
        description=(
            'Restated observation content on the surviving evidence. If the surviving '
            'evidence still supports the original observation, you may restate it with '
            'unchanged content; otherwise rewrite it to reflect only what the evidence supports.'
        ),
    )
    title: str = Field(
        description='Observation title — unchanged unless the surviving evidence no longer supports it.',
    )
    should_drop: bool = Field(
        default=False,
        description=(
            'True if the surviving evidence no longer supports any coherent observation. '
            'Use sparingly; the caller applies a retention guardrail and may override.'
        ),
    )
    dropped_reason: str | None = Field(
        default=None,
        description='When should_drop=True, a one-line rationale recorded in logs/metrics.',
    )

    @model_validator(mode='after')
    def _non_empty_when_keeping(self) -> 'RefreshedObservation':
        """Reject blank content/title when should_drop=False.

        Without this, an LLM that returns ``RefreshedObservation(content='',
        title='', should_drop=False)`` would persist a degenerate observation.
        Raising forces the caller's `_invoke_refresh_signature_with_context`
        to treat the result as malformed — scheduler marks the task failed,
        retry_count++, eventually DEAD_LETTERs.
        """
        if not self.should_drop:
            if not (self.content or '').strip():
                raise ValueError('RefreshedObservation.content is empty but should_drop=False')
            if not (self.title or '').strip():
                raise ValueError('RefreshedObservation.title is empty but should_drop=False')
        return self


class RefreshObservationSignature(dspy.Signature):
    """Re-synthesise a single observation on a strictly-smaller evidence set.

    Some of the original supporting memories have been pruned (deprioritized or stale).
    State only what the surviving evidence supports; if support is insufficient,
    set should_drop=True with a brief dropped_reason.

    STRICT RULE: All output text MUST be in English.
    """

    observation: ReflectObservationContext = dspy.InputField(
        desc='The existing observation under review (title + content).'
    )
    surviving_evidence: list[ReflectMemoryContext] = dspy.InputField(
        desc='Memory units that still support the observation; may be empty.'
    )

    refreshed: RefreshedObservation = dspy.OutputField(
        desc='Updated observation, or a drop signal if surviving evidence is insufficient.'
    )
