"""F10 LLM check factories — build runnable RunLLMCheck callables.

Each factory binds a DSPy ``LM`` and signature into a coroutine matching
:class:`memex_core.services.lint_llm.RunLLMCheck`. The coroutine:

1. Loads the audited unit's text + a sample of related/corpus units via
   the provided ``AsyncSession``.
2. Calls the DSPy signature through
   :func:`memex_core.llm.run_dspy_operation` (which adds circuit-breaker
   pre-flight + Prometheus metrics + OTel span).
3. Returns an :class:`memex_core.services.lint_llm.LLMLintFinding` if the
   signature flags an issue, else ``None``.

References: RFC-006 §"LLM check types — DSPy signatures",
§"Output shape on MaintenanceProposal.evidence".
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import dspy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import memex_core.llm as _llm
from memex_core.memory.lint_llm.signatures import (
    CheckSchemaDrift,
    CheckSemanticContradiction,
)
from memex_core.memory.lint_llm.surprise import compute_unit_surprise
from memex_core.memory.lint_llm.types import (
    CheckContext,
    LLMLintFinding,
    PolarityLiteral,
    RunLLMCheck,
)
from memex_core.memory.sql_models import LintType

logger = logging.getLogger('memex.core.memory.lint_llm.checks')


_RULE_LLM_SEMANTIC_CONTRADICTION = 'llm_semantic_contradiction'
_RULE_LLM_SCHEMA_DRIFT = 'llm_schema_drift'

_SUGGESTED_ACTION_CONTRADICTION = (
    'Review for semantic contradiction with the cited related unit(s); '
    'consider deprioritising the older statement or supersedeing one with '
    'a corrected note.'
)
_SUGGESTED_ACTION_DRIFT = (
    'Review for structural / schema drift; consider re-ingesting the unit '
    'in the corpus-norm format or annotating the divergence intentionally.'
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LOAD_UNIT_TEXT_SQL = text("""
    SELECT text FROM memory_units WHERE id = :unit_id
""")


_LOAD_TOP_K_RELATED_SQL = text("""
    SELECT m.id, m.text
    FROM memory_units m
    WHERE m.vault_id = :vault_id
      AND m.id != :unit_id
      AND m.status = 'active'
      AND m.embedding IS NOT NULL
      AND (SELECT embedding FROM memory_units WHERE id = :unit_id) IS NOT NULL
    ORDER BY m.embedding <=> (SELECT embedding FROM memory_units WHERE id = :unit_id)
    LIMIT :k
""")


_LOAD_RANDOM_CORPUS_SAMPLE_SQL = text("""
    SELECT text FROM memory_units
    WHERE vault_id = :vault_id
      AND id != :unit_id
      AND status = 'active'
    ORDER BY random()
    LIMIT :k
""")


async def _load_unit_text(session: AsyncSession, unit_id: UUID) -> str | None:
    result = await session.execute(_LOAD_UNIT_TEXT_SQL, {'unit_id': str(unit_id)})
    row = result.first()
    return None if row is None else str(row.text)


async def _load_top_k_related(
    session: AsyncSession, unit_id: UUID, vault_id: UUID, *, k: int
) -> list[tuple[UUID, str]]:
    result = await session.execute(
        _LOAD_TOP_K_RELATED_SQL,
        {'unit_id': str(unit_id), 'vault_id': str(vault_id), 'k': k},
    )
    return [(row.id, str(row.text)) for row in result]


async def _load_random_sample(
    session: AsyncSession, unit_id: UUID, vault_id: UUID, *, k: int
) -> list[str]:
    result = await session.execute(
        _LOAD_RANDOM_CORPUS_SAMPLE_SQL,
        {'unit_id': str(unit_id), 'vault_id': str(vault_id), 'k': k},
    )
    return [str(row.text) for row in result]


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def make_semantic_contradiction_check(
    lm: dspy.LM,
    *,
    k: int = 8,
    surprise_k: int | None = None,
) -> RunLLMCheck:
    """Build a ``RunLLMCheck`` that wraps :class:`CheckSemanticContradiction`.

    The returned coroutine loads the audited unit's text + top-k related
    units (same pgvector neighbourhood the surprise gate uses) and asks the
    LLM whether any of them is a sentence-level inversion of the audited
    unit. Surprise is recomputed inside the check so the evidence payload
    can carry it for downstream filtering / diagnostics.

    F10b: when the orchestrator (:meth:`LintLLMService.maybe_run`) attaches a
    :class:`CheckContext` carrying a ``PolarityResult`` (because the
    cosine-OR-polarity gate cleared via the polarity branch), the check
    forwards the argmax label to the DSPy signature as ``polarity_hint``
    and adds the probabilities to ``extra_evidence``. With no context, the
    check is byte-identical to its F10 behaviour.
    """
    predictor = dspy.Predict(CheckSemanticContradiction)
    sk = surprise_k if surprise_k is not None else k

    async def _check(
        unit_id: UUID,
        vault_id: UUID,
        session: AsyncSession,
        context: CheckContext | None = None,
    ) -> LLMLintFinding | None:
        unit_text = await _load_unit_text(session, unit_id)
        if unit_text is None:
            logger.warning('F10 semantic-contradiction: unit %s missing — skipping', unit_id)
            return None

        related = await _load_top_k_related(session, unit_id, vault_id, k=k)
        if len(related) < 2:
            return None

        related_ids = [rid for rid, _ in related]
        related_texts = [t for _, t in related]
        score = await compute_unit_surprise(unit_id, vault_id, session, k=sk)

        polarity_hint: PolarityLiteral | None = None
        polarity_result = context.polarity if context is not None else None
        if polarity_result is not None:
            polarity_hint = polarity_result.label.value  # type: ignore[assignment]

        prediction = await _llm.run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={
                'unit_text': unit_text,
                'related_units_text': related_texts,
                'polarity_hint': polarity_hint,
            },
            operation_name='lint_llm.semantic_contradiction',
        )

        if not getattr(prediction, 'has_contradiction', False):
            return None

        contradicting_indices: list[int] = list(
            getattr(prediction, 'contradiction_with_unit_indices', []) or []
        )
        cited_unit_ids = [
            str(related_ids[i]) for i in contradicting_indices if 0 <= i < len(related_ids)
        ]

        extra_evidence: dict[str, Any] = {}
        if polarity_result is not None:
            extra_evidence['polarity_label'] = polarity_result.label.value
            extra_evidence['polarity_contradiction_prob'] = polarity_result.contradiction_prob
            extra_evidence['polarity_entailment_prob'] = polarity_result.entailment_prob
            extra_evidence['polarity_neutral_prob'] = polarity_result.neutral_prob

        return LLMLintFinding(
            rule_name=_RULE_LLM_SEMANTIC_CONTRADICTION,
            check_type='semantic_contradiction',
            target_type='memory_unit',
            target_id=str(unit_id),
            suggested_action=_SUGGESTED_ACTION_CONTRADICTION,
            surprise_score=score,
            explanation=str(getattr(prediction, 'explanation', '') or ''),
            related_unit_ids=cited_unit_ids,
            extra_evidence=extra_evidence,
            lint_type=LintType.QUALITY,
        )

    return _check


def make_schema_drift_check(
    lm: dspy.LM,
    *,
    k: int = 8,
    surprise_k: int | None = None,
) -> RunLLMCheck:
    """Build a ``RunLLMCheck`` that wraps :class:`CheckSchemaDrift`.

    Loads the audited unit's text + a random sample of corpus units (NOT
    the surprise neighbourhood — drift is a comparison against the *typical*
    structure, not the *similar* content) and asks the LLM whether the
    audited unit structurally diverges.
    """
    predictor = dspy.Predict(CheckSchemaDrift)
    sk = surprise_k if surprise_k is not None else k

    async def _check(
        unit_id: UUID,
        vault_id: UUID,
        session: AsyncSession,
        context: CheckContext | None = None,
    ) -> LLMLintFinding | None:
        unit_text = await _load_unit_text(session, unit_id)
        if unit_text is None:
            logger.warning('F10 schema-drift: unit %s missing — skipping', unit_id)
            return None

        sample = await _load_random_sample(session, unit_id, vault_id, k=k)
        if len(sample) < 2:
            return None

        score = await compute_unit_surprise(unit_id, vault_id, session, k=sk)

        prediction = await _llm.run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={
                'unit_text': unit_text,
                'sample_corpus_units': sample,
            },
            operation_name='lint_llm.schema_drift',
        )

        if not getattr(prediction, 'has_drift', False):
            return None

        drift_kind = str(getattr(prediction, 'drift_kind', '') or 'other')
        return LLMLintFinding(
            rule_name=_RULE_LLM_SCHEMA_DRIFT,
            check_type='schema_drift',
            target_type='memory_unit',
            target_id=str(unit_id),
            suggested_action=_SUGGESTED_ACTION_DRIFT,
            surprise_score=score,
            explanation=str(getattr(prediction, 'explanation', '') or ''),
            related_unit_ids=[],
            extra_evidence={'drift_kind': drift_kind},
            lint_type=LintType.SCHEMA,
        )

    return _check
