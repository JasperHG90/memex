"""
DSPy prompts and signatures for the Hindsight Reflect Loop.
"""

import dspy
from pydantic import BaseModel, Field

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


class ComparePhaseOutput(BaseModel):
    observations: list[ValidatedObservation] = Field(
        description='The final merged list of observations.'
    )
    changes_summary: dict = Field(description='Summary of what was added, merged, or removed.')


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

    The 'evidence_context' list contains all unique facts/evidence referenced by the observations.
    Observations refer to these facts by their 0-based index in 'evidence_context'.

    STRICT RULE: All output observations MUST be in English.
    """

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
