"""DSPy signatures for the surprise-gated LLM lint.

Two checks:

- :class:`CheckSemanticContradiction` — does this unit contradict any of
  its top-k peers?
- :class:`CheckSchemaDrift` — does this unit's structure (date format,
  ID style, schema) diverge from the corpus norm?

Both are wrapped in the circuit breaker via
:func:`memex_core.llm.run_dspy_operation` at the factory layer
(``checks.make_semantic_contradiction_check`` /
``make_schema_drift_check``).

Polarity discrimination is bridged by the NLI classifier — see
``memex_core.memory.lint_llm.polarity`` and the OR'd
``surprise.gate_passes`` composition. The NLI label is passed through
to :class:`CheckSemanticContradiction` via ``polarity_hint`` so the LLM
has both the topical-novelty signal (cosine surprise) and the
sign-of-meaning signal (NLI label) available.
"""

from __future__ import annotations

from typing import Any, Literal

import dspy
from pydantic import field_validator

from memex_core.memory.lint_llm.types import PolarityLiteral

WinnerLiteral = Literal['unit_a', 'unit_b', 'inconclusive']
LoserLiteral = Literal['unit_a', 'unit_b', 'none']
ResolutionActionLiteral = Literal[
    'mark_loser_stale',
    'supersede_loser_note',
    'refine_not_contradict',
    'inconclusive',
]


class CheckSemanticContradiction(dspy.Signature):
    """Identify whether ``unit_text`` semantically contradicts any of the
    related units provided.

    A contradiction is a sentence-level inversion of meaning, not topical
    drift or unrelated content. Examples:

    - "User prefers staging" vs "User prefers production" → contradiction.
    - "User prefers staging" vs "User uses Postgres on staging" → no
      contradiction (compatible facts).

    STRICT RULE: only emit ``has_contradiction=True`` when the inversion is
    explicit and unambiguous. If the units are merely about different
    aspects of the same topic, return False.
    """

    unit_text: str = dspy.InputField(
        desc='The text of the memory unit being audited for contradictions.'
    )
    related_units_text: list[str] = dspy.InputField(
        desc='Top-k related units from the same vault, indexed 0..k-1.'
    )
    polarity_hint: PolarityLiteral | None = dspy.InputField(
        desc=(
            'NLI classifier label (entailment / neutral / contradiction) '
            'when the surprise gate cleared via the polarity branch only. '
            'Use as a HINT, not a hard signal — the LLM is still the final '
            'judge. Empty / None when the cosine surprise gate alone fired.'
        ),
    )
    has_contradiction: bool = dspy.OutputField(
        desc='True if at least one related unit contradicts the audited unit.'
    )
    contradiction_with_unit_indices: list[int] = dspy.OutputField(
        desc=(
            'Zero-based indices into related_units_text identifying which '
            'units contradict the audited unit. Empty when has_contradiction '
            'is False.'
        )
    )
    explanation: str = dspy.OutputField(
        desc=(
            'One- or two-sentence English explanation of why the units '
            'contradict, citing the sentence-level inversion. Empty when '
            'has_contradiction is False.'
        )
    )


class CheckSchemaDrift(dspy.Signature):
    """Detect when a memory unit's structure diverges from corpus norms.

    Schema drift is purely structural — date format, identifier style,
    JSON-vs-prose vs table form, naming conventions, and so on. It is NOT
    about the *content* being unusual; that is the surprise gate's job.

    Examples:

    - Most units use ``YYYY-MM-DD``; this unit uses ``MM/DD/YYYY`` →
      ``has_drift=True``, ``drift_kind='date_format'``.
    - Most units cite IDs as ``user-12345``; this unit uses ``USR-12345`` →
      ``has_drift=True``, ``drift_kind='id_style'``.
    - This unit is structurally identical to the corpus norm but covers a
      different topic → ``has_drift=False`` (the surprise gate already
      handled topical anomalies).

    STRICT RULE: leave ``has_drift=False`` unless the structural mismatch
    is concrete and present in at least one corpus sample for comparison.
    """

    unit_text: str = dspy.InputField(
        desc='The text of the memory unit being audited for schema drift.'
    )
    sample_corpus_units: list[str] = dspy.InputField(
        desc=(
            'Random sample of recent units from the same vault, used as the '
            'structural reference. Indexed 0..k-1.'
        )
    )
    has_drift: bool = dspy.OutputField(
        desc='True if the unit structurally diverges from the corpus norm.'
    )
    drift_kind: str = dspy.OutputField(
        desc=(
            "One of: 'date_format' | 'id_style' | 'schema' | 'naming' | "
            "'other'. Empty when has_drift is False."
        )
    )
    explanation: str = dspy.OutputField(
        desc=(
            'One- or two-sentence English explanation of the structural '
            'mismatch. Empty when has_drift is False.'
        )
    )


class ProposeContradictionWinner(dspy.Signature):
    """Given two memory units flagged by FSFM lint as in tension, propose
    which one should win, the recommended action, and a calibrated
    confidence.

    Inputs include the two units' text plus per-side recency and source
    metadata so the LLM can reason about authority and freshness. The
    fsfm_evidence field carries the upstream finding's evidence payload
    (flag_reason, component scores) so the proposal is anchored in the
    same signals the human reviewer sees.

    STRICT RULES:
    - Only propose ``mark_loser_stale`` when one unit is clearly wrong
      and the other clearly right. The loser must be the older / lower-
      authority unit unless authority strongly inverts.
    - Use ``supersede_loser_note`` when the winner's source note is a
      genuinely newer recording of the same fact and the agent wants the
      parent note marked superseded (not just the unit).
    - Use ``refine_not_contradict`` when the units are compatible
      refinements (different aspects of the same fact), NOT semantic
      inversions.
    - Use ``inconclusive`` when the signal is too weak to judge.
    """

    unit_a_text: str = dspy.InputField(desc='Text of the first memory unit (unit_a).')
    unit_b_text: str = dspy.InputField(desc='Text of the second memory unit (unit_b).')
    unit_a_created_at: str = dspy.InputField(
        desc='ISO-8601 created_at of unit_a (string form for prompt-friendly serialisation).'
    )
    unit_b_created_at: str = dspy.InputField(desc='ISO-8601 created_at of unit_b.')
    unit_a_source_credibility: float = dspy.InputField(
        desc='Memory Worth posterior of unit_a (0..1). Higher = more credible.'
    )
    unit_b_source_credibility: float = dspy.InputField(
        desc='Memory Worth posterior of unit_b (0..1). Higher = more credible.'
    )
    unit_a_source_authority: str = dspy.InputField(
        desc=(
            "Free-text authority label for unit_a's source note "
            "(e.g. 'official-doc', 'chat-log', 'user-edit'). Empty when unknown."
        )
    )
    unit_b_source_authority: str = dspy.InputField(
        desc="Free-text authority label for unit_b's source note. Empty when unknown."
    )
    fsfm_evidence: dict[str, Any] = dspy.InputField(
        desc=(
            'Evidence payload from the upstream FSFM finding — includes '
            'flag_reason and the component score breakdown.'
        )
    )

    winner_id: WinnerLiteral = dspy.OutputField(
        desc="Which unit wins: 'unit_a' | 'unit_b' | 'inconclusive'."
    )
    loser_id: LoserLiteral = dspy.OutputField(
        desc=("Which unit loses: 'unit_a' | 'unit_b' | 'none'. 'none' when winner is inconclusive.")
    )
    rationale: str = dspy.OutputField(
        desc='One- or two-sentence English justification anchored in the inputs.'
    )
    confidence: float = dspy.OutputField(
        desc='Calibrated confidence in the proposal (clamped to [0.0, 1.0]).'
    )
    action: ResolutionActionLiteral = dspy.OutputField(
        desc=(
            "Recommended resolution action: 'mark_loser_stale' | "
            "'supersede_loser_note' | 'refine_not_contradict' | 'inconclusive'."
        )
    )

    @field_validator('confidence', mode='before')
    @classmethod
    def _clamp_confidence(cls, v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        if f < 0.0:
            return 0.0
        if f > 1.0:
            return 1.0
        return f
