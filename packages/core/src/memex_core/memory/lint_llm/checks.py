"""LLM check factories — build runnable RunLLMCheck callables.

Each factory binds a DSPy ``LM`` and signature into a coroutine matching
:class:`memex_core.services.lint_llm.RunLLMCheck`. The coroutine:

1. Loads the audited unit's text + a sample of related/corpus units via
   the provided ``AsyncSession``.
2. Calls the DSPy signature through
   :func:`memex_core.llm.run_dspy_operation` (which adds circuit-breaker
   pre-flight + Prometheus metrics + OTel span).
3. Returns an :class:`memex_core.services.lint_llm.LLMLintFinding` if the
   signature flags an issue, else ``None``.
"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

import dspy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import memex_core.llm as _llm
from memex_core.memory.lint_llm.signatures import (
    CheckSchemaDrift,
    CheckSemanticContradiction,
    ProposeContradictionWinner,
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
_RULE_PROPOSE_CONTRADICTION_WINNER = 'propose_contradiction_winner'

_QUALIFYING_FLAG_REASONS: frozenset[str] = frozenset(
    {
        'low_credibility_contradiction_only',
        'components_disagree',
    }
)

_SUGGESTED_ACTION_PROPOSE_WINNER = (
    'Review the proposed winner and call memex_lint_apply_winner to apply '
    'the recommended resolution, or memex_lint_reverse_winner if the '
    'mutation has already been applied and needs to be undone.'
)

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

    When the orchestrator (:meth:`LintLLMService.maybe_run`) attaches a
    :class:`CheckContext` carrying a ``PolarityResult`` (because the
    cosine-OR-polarity gate cleared via the polarity branch), the check
    forwards the argmax label to the DSPy signature as ``polarity_hint``
    and adds the probabilities to ``extra_evidence``. With no context, the
    check is byte-identical to its original behaviour.
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
            logger.warning('Semantic-contradiction: unit %s missing — skipping', unit_id)
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
            polarity_hint = cast(PolarityLiteral, polarity_result.label.value)

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
            logger.warning('Schema-drift: unit %s missing — skipping', unit_id)
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


_LOAD_FSFM_FINDING_FOR_UNIT_SQL = text("""
    SELECT id::text AS finding_id, evidence
    FROM maintenance_proposals
    WHERE rule_name = 'composite_deprioritize_candidate'
      AND target_type = 'memory_unit'
      AND target_id = :unit_id
      AND status = 'pending'
      AND (vault_id = :vault_id OR vault_id IS NULL)
    ORDER BY created_at DESC
    LIMIT 1
""")


_EXISTING_WINNER_PROPOSAL_SQL = text("""
    SELECT 1
    FROM maintenance_proposals
    WHERE rule_name = 'propose_contradiction_winner'
      AND target_type = 'memory_unit'
      AND target_id = :unit_id
      AND status = 'pending'
      AND (vault_id = :vault_id OR vault_id IS NULL)
    LIMIT 1
""")


_LOAD_TOP_CONTRADICTS_LINK_SQL = text("""
    SELECT
        ml.id::text AS link_id,
        ml.from_unit_id::text AS source_unit_id,
        ml.weight AS weight,
        ml.created_at AS link_created_at,
        src.text AS source_text,
        src.created_at AS source_created_at,
        src.confidence AS source_confidence,
        ((src.success_co_count + 1.0) /
         (src.success_co_count + src.failure_co_count + 2)) AS source_mw,
        src.note_id::text AS source_note_id,
        src_note.metadata AS source_note_metadata
    FROM memory_links ml
    JOIN memory_units src ON src.id = ml.from_unit_id
    LEFT JOIN notes src_note ON src_note.id = src.note_id
    WHERE ml.to_unit_id = :unit_id
      AND ml.vault_id = :vault_id
      AND src.vault_id = :vault_id
      AND ml.link_type = 'contradicts'
      AND src.status = 'active'
    ORDER BY ml.weight DESC, ml.created_at DESC
    LIMIT 1
""")


_LOAD_UNIT_WITH_NOTE_SQL = text("""
    SELECT
        mu.text AS unit_text,
        mu.created_at AS unit_created_at,
        mu.confidence AS unit_confidence,
        ((mu.success_co_count + 1.0) /
         (mu.success_co_count + mu.failure_co_count + 2)) AS unit_mw,
        mu.note_id::text AS note_id,
        n.metadata AS note_metadata
    FROM memory_units mu
    LEFT JOIN notes n ON n.id = mu.note_id
    WHERE mu.id = :unit_id
""")


def _authority_from_metadata(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ''
    for key in ('authority', 'source_authority', 'template'):
        v = metadata.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ''


def make_propose_contradiction_winner_check(
    lm: dspy.LM,
    *,
    min_confidence: float = 0.6,
) -> RunLLMCheck:
    """Build a ``RunLLMCheck`` that wraps :class:`ProposeContradictionWinner`.

    Operates as a follow-on to FSFM lint: only fires on units that already
    carry a pending ``composite_deprioritize_candidate`` finding whose
    ``flag_reason`` ∈ {``low_credibility_contradiction_only``,
    ``components_disagree``}. Locates the highest-pressure inbound
    ``contradicts`` link, loads both units and their source-note
    authority labels, and asks the LLM to nominate a winner + an action.

    A finding is emitted for *any* verdict the LLM returns — including
    ``inconclusive`` — so the audit trail records that lint ran. Confidence
    no longer drives ``None``: a definitive ``unit_a`` / ``unit_b`` verdict
    with ``confidence < min_confidence`` is downgraded to ``inconclusive``
    (so the apply path is blocked) but still emitted.

    Returns ``None`` only on the pre-check filters: no qualifying FSFM
    finding for the unit, an existing pending winner-proposal for the
    same unit, or no inbound contradicts-link peer row.
    """
    predictor = dspy.Predict(ProposeContradictionWinner)

    async def _check(
        unit_id: UUID,
        vault_id: UUID,
        session: AsyncSession,
        context: CheckContext | None = None,
    ) -> LLMLintFinding | None:
        fsfm_row = (
            await session.execute(
                _LOAD_FSFM_FINDING_FOR_UNIT_SQL,
                {'unit_id': str(unit_id), 'vault_id': str(vault_id)},
            )
        ).first()
        if fsfm_row is None:
            return None
        evidence = fsfm_row.evidence or {}
        flag_reason = str(evidence.get('flag_reason') or '')
        if flag_reason not in _QUALIFYING_FLAG_REASONS:
            return None

        existing = (
            await session.execute(
                _EXISTING_WINNER_PROPOSAL_SQL,
                {'unit_id': str(unit_id), 'vault_id': str(vault_id)},
            )
        ).first()
        if existing is not None:
            return None

        peer_row = (
            await session.execute(
                _LOAD_TOP_CONTRADICTS_LINK_SQL,
                {'unit_id': str(unit_id), 'vault_id': str(vault_id)},
            )
        ).first()
        if peer_row is None:
            return None

        loser_row = (
            await session.execute(
                _LOAD_UNIT_WITH_NOTE_SQL,
                {'unit_id': str(unit_id)},
            )
        ).first()
        if loser_row is None or loser_row.unit_text is None:
            return None

        prediction = await _llm.run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={
                'unit_a_text': str(peer_row.source_text or ''),
                'unit_b_text': str(loser_row.unit_text),
                'unit_a_created_at': (
                    peer_row.source_created_at.isoformat()
                    if peer_row.source_created_at is not None
                    else ''
                ),
                'unit_b_created_at': (
                    loser_row.unit_created_at.isoformat()
                    if loser_row.unit_created_at is not None
                    else ''
                ),
                'unit_a_source_credibility': float(peer_row.source_mw or 0.0),
                'unit_b_source_credibility': float(loser_row.unit_mw or 0.0),
                'unit_a_source_authority': _authority_from_metadata(peer_row.source_note_metadata),
                'unit_b_source_authority': _authority_from_metadata(loser_row.note_metadata),
                'fsfm_evidence': dict(evidence),
            },
            operation_name='lint_llm.propose_contradiction_winner',
        )

        confidence = float(getattr(prediction, 'confidence', 0.0) or 0.0)
        action = str(getattr(prediction, 'action', 'inconclusive') or 'inconclusive')
        winner_id_label = str(getattr(prediction, 'winner_id', 'inconclusive') or 'inconclusive')
        loser_id_label = str(getattr(prediction, 'loser_id', 'none') or 'none')
        rationale = str(getattr(prediction, 'rationale', '') or '')

        # Confidence gate is inclusive on the threshold: ``confidence >=
        # min_confidence`` passes; strictly below ``min_confidence`` is
        # downgraded to ``inconclusive`` (not dropped) so the audit trail
        # survives while the apply path is blocked.
        if winner_id_label in ('unit_a', 'unit_b') and confidence < min_confidence:
            winner_id_label = 'inconclusive'
            loser_id_label = 'none'
            action = 'inconclusive'

        if winner_id_label == 'inconclusive':
            action = 'inconclusive'
            loser_id_label = 'none'

        if winner_id_label == 'unit_a':
            resolved_winner_unit_id = peer_row.source_unit_id
            resolved_loser_unit_id = str(unit_id)
        elif winner_id_label == 'unit_b':
            resolved_winner_unit_id = str(unit_id)
            resolved_loser_unit_id = peer_row.source_unit_id
        else:
            resolved_winner_unit_id = None
            resolved_loser_unit_id = str(unit_id)

        target_id = resolved_loser_unit_id or str(unit_id)

        extra_evidence: dict[str, Any] = {
            'linked_to_finding': str(fsfm_row.finding_id),
            'flag_reason': flag_reason,
            'winner_id': winner_id_label,
            'loser_id': loser_id_label,
            'winner_unit_id': resolved_winner_unit_id,
            'loser_unit_id': resolved_loser_unit_id,
            'peer_unit_id': peer_row.source_unit_id,
            'link_id': peer_row.link_id,
            'confidence': confidence,
            'action': action,
            'rationale': rationale,
        }

        return LLMLintFinding(
            rule_name=_RULE_PROPOSE_CONTRADICTION_WINNER,
            check_type='propose_contradiction_winner',
            target_type='memory_unit',
            target_id=target_id,
            suggested_action=_SUGGESTED_ACTION_PROPOSE_WINNER,
            surprise_score=0.0,
            explanation=rationale,
            related_unit_ids=[peer_row.source_unit_id],
            extra_evidence=extra_evidence,
            lint_type=LintType.QUALITY,
        )

    return _check
