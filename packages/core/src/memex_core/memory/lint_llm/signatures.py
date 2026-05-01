"""F10 DSPy signatures for the surprise-gated LLM lint.

Two checks per RFC-006 §"LLM check types — DSPy signatures":

- :class:`CheckSemanticContradiction` — does this unit contradict any of
  its top-k peers?
- :class:`CheckSchemaDrift` — does this unit's structure (date format,
  ID style, schema) diverge from the corpus norm?

Both are wrapped in F10's circuit breaker via
:func:`memex_core.llm.run_dspy_operation` at the factory layer
(``checks.make_semantic_contradiction_check`` /
``make_schema_drift_check``).

Polarity discrimination is a known structural limit of MiniLM-L12
embeddings — POC-F10 found that the surprise gate cannot raise polarity
inversions to threshold. F10 ships these signatures with that constraint
in place; Tier B will revisit via NLI-scored gate input. See
``pocs/002-f10-surprise-threshold/result.md``.
"""

from __future__ import annotations

import dspy


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
