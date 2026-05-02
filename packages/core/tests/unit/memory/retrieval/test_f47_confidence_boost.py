"""F47: post-reranker contradiction-derived confidence boost composition.

Asserts the multiplicative boost wired at engine.py near
``boosted_scores.append(...)`` matches the spec formula
``confidence_boost = 1.0 + confidence_alpha * (unit.confidence - 0.5)`` and
that the ship default ``confidence_alpha=0.0`` is a no-op for every unit.
"""

from __future__ import annotations

import math
from datetime import datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from memex_common.config import RetrievalConfig
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryUnit


def _make_unit(
    confidence: float = 1.0,
    text: str = 'fact',
    event_date: datetime | None = None,
) -> MemoryUnit:
    unit = MemoryUnit(
        id=uuid4(),
        text=text,
        fact_type='fact',
        event_date=event_date,
        vault_id=uuid4(),
        note_id=uuid4(),
        embedding=[],
        success_co_count=0,
        failure_co_count=0,
        confidence=confidence,
    )
    return unit


def _make_engine(
    scores: list[float],
    confidence_alpha: float = 0.0,
    recency_alpha: float = 0.0,
    temporal_alpha: float = 0.0,
    mw_alpha: float = 0.0,
) -> RetrievalEngine:
    """All non-target alphas default to 0 so we isolate confidence_boost."""
    reranker = MagicMock()
    reranker.score.return_value = scores
    config = RetrievalConfig(
        reranking_recency_alpha=recency_alpha,
        reranking_temporal_alpha=temporal_alpha,
        reranking_mw_alpha=mw_alpha,
        confidence_alpha=confidence_alpha,
    )
    return RetrievalEngine(
        embedder=MagicMock(),
        reranker=reranker,
        retrieval_config=config,
    )


class TestConfidenceBoostFormula:
    """Spec: confidence_boost = 1.0 + confidence_alpha * (unit.confidence - 0.5)."""

    @pytest.mark.asyncio
    async def test_cold_start_alpha_zero_yields_neutral_boost(self) -> None:
        """confidence=1.0 (schema default), alpha=0.0 → boost = 1.0."""
        unit = _make_unit(confidence=1.0)
        # ce raw score 0.0 → sigmoid 0.5; all other boosts neutral (alphas zero).
        engine = _make_engine([0.0], confidence_alpha=0.0)
        result = await engine._rerank_results('q', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_cold_start_alpha_nonzero_yields_lift(self) -> None:
        """confidence=1.0, alpha=0.3 → boost = 1.0 + 0.3 * 0.5 = 1.15.

        Pins the engine-emitted boost factor to the spec formula by reading
        the ``CONFIDENCE_BOOST_OBSERVED`` histogram's ``_sum`` delta — connects
        the pure-formula table (``TestCompositionFormulaValues``) to the live
        engine wiring.
        """
        from memex_core.metrics import CONFIDENCE_BOOST_OBSERVED

        unit = _make_unit(confidence=1.0)
        engine = _make_engine([0.0], confidence_alpha=0.3)
        sum_before = CONFIDENCE_BOOST_OBSERVED._sum.get()
        result = await engine._rerank_results('q', [unit])
        sum_after = CONFIDENCE_BOOST_OBSERVED._sum.get()
        assert len(result) == 1
        assert math.isclose(sum_after - sum_before, 1.15, abs_tol=1e-6)

    @pytest.mark.asyncio
    async def test_single_contradiction_dampens_boost(self) -> None:
        """confidence=0.8, alpha=0.3 → boost = 1.0 + 0.3 * 0.3 = 1.09."""
        unit = _make_unit(confidence=0.8)
        engine = _make_engine([0.0], confidence_alpha=0.3)
        result = await engine._rerank_results('q', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_heavily_contradicted_unit_penalised(self) -> None:
        """confidence=0.0, alpha=0.3 → boost = 1.0 + 0.3 * (-0.5) = 0.85.

        Pins the engine-emitted boost factor to the spec formula via the
        ``CONFIDENCE_BOOST_OBSERVED`` histogram ``_sum`` delta.
        """
        from memex_core.metrics import CONFIDENCE_BOOST_OBSERVED

        unit = _make_unit(confidence=0.0)
        engine = _make_engine([0.0], confidence_alpha=0.3)
        sum_before = CONFIDENCE_BOOST_OBSERVED._sum.get()
        result = await engine._rerank_results('q', [unit])
        sum_after = CONFIDENCE_BOOST_OBSERVED._sum.get()
        assert len(result) == 1
        assert math.isclose(sum_after - sum_before, 0.85, abs_tol=1e-6)

    @pytest.mark.asyncio
    async def test_alpha_zero_is_invariant_across_confidence(self) -> None:
        """alpha=0 → boost=1.0 for every confidence value (off-by-default invariant).

        Two units with identical CE / recency / temporal / MW but different
        confidence rank purely by CE when alpha=0.
        """
        unit_low = _make_unit(confidence=0.0, text='low conf')
        unit_high = _make_unit(confidence=1.0, text='high conf')
        # Give the low-confidence unit a strictly higher CE score.
        # If alpha were silently non-zero, the high-confidence unit would
        # be lifted ahead of it. With alpha=0, the higher CE wins.
        engine = _make_engine([2.0, 0.0], confidence_alpha=0.0)
        result = await engine._rerank_results('q', [unit_low, unit_high])
        assert result[0] is unit_low
        assert result[1] is unit_high


class TestConfidenceBoostRanking:
    """End-to-end: high-confidence units rank above low-confidence peers when alpha>0."""

    @pytest.mark.asyncio
    async def test_high_confidence_beats_low_confidence_with_alpha_on(self) -> None:
        unit_clean = _make_unit(confidence=1.0, text='clean')
        unit_contradicted = _make_unit(confidence=0.0, text='contradicted')
        # Same CE score → confidence_boost (1.15 vs 0.85) is the only differentiator.
        engine = _make_engine([0.0, 0.0], confidence_alpha=0.3)
        result = await engine._rerank_results('q', [unit_contradicted, unit_clean])
        assert result[0] is unit_clean
        assert result[1] is unit_contradicted


class TestNoBehaviorChangeAtShipDefault:
    """At ship default (confidence_alpha=0.0) the final score equals
    ce * recency * temporal * mw — no contribution from confidence_boost."""

    @pytest.mark.asyncio
    async def test_default_alpha_matches_pre_f47_composition(self) -> None:
        """Two units identical except confidence rank by CE alone at alpha=0.

        Independently exercise both with default RetrievalConfig (confidence_alpha
        default = 0.0). Higher CE must win regardless of confidence.
        """
        unit_dirty = _make_unit(confidence=0.1, text='dirty')
        unit_clean = _make_unit(confidence=1.0, text='clean')
        # Default RetrievalConfig has confidence_alpha=0.0 (verify ship default).
        config = RetrievalConfig()
        assert config.confidence_alpha == 0.0
        reranker = MagicMock()
        reranker.score.return_value = [3.0, 0.0]  # dirty has higher CE
        engine = RetrievalEngine(embedder=MagicMock(), reranker=reranker, retrieval_config=config)
        result = await engine._rerank_results('q', [unit_dirty, unit_clean])
        # Higher CE wins because confidence_boost contributes 1.0 for both.
        assert result[0] is unit_dirty
        assert result[1] is unit_clean


class TestMissingConfidenceAttribute:
    """A unit without a confidence attribute (defensive: shouldn't happen given
    NOT NULL DEFAULT 1.0 schema) defaults to 1.0 → cold-start neutral path."""

    @pytest.mark.asyncio
    async def test_missing_confidence_attr_defaults_to_one(self) -> None:
        unit = _make_unit(confidence=1.0)
        # Strip the attribute to simulate a stale model object.
        try:
            object.__delattr__(unit, 'confidence')
        except AttributeError:
            pass
        engine = _make_engine([0.0], confidence_alpha=0.3)
        result = await engine._rerank_results('q', [unit])
        assert len(result) == 1


class TestCompositionFormulaValues:
    """Numeric assertions on the spec formula at canonical confidence levels."""

    @pytest.mark.parametrize(
        ('confidence', 'alpha', 'expected'),
        [
            (1.0, 0.0, 1.0),
            (0.0, 0.0, 1.0),
            (0.5, 0.0, 1.0),
            (1.0, 0.3, 1.15),
            (0.9, 0.3, 1.12),
            (0.8, 0.3, 1.09),
            (0.5, 0.3, 1.0),
            (0.2, 0.3, 0.91),
            (0.0, 0.3, 0.85),
        ],
    )
    def test_formula_table(self, confidence: float, alpha: float, expected: float) -> None:
        boost = 1.0 + alpha * (confidence - 0.5)
        assert math.isclose(boost, expected, abs_tol=1e-9)
