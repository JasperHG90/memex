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

import dspy

from memex_core.memory.lint_llm.types import PolarityLiteral


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
