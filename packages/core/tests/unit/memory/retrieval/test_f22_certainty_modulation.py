"""F22 — certainty-modulated F47 boost composition.

When ``certainty_modulation_enabled = True`` the F47 confidence boost is
multiplied by ``certainty = 1 - variance / MAX_VARIANCE``. With the ship
default ``certainty_modulation_enabled = False`` (verified here as a
regression guard), the boost is bit-for-bit identical to the F47 baseline.
"""

from __future__ import annotations

import math
from datetime import datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from memex_common.config import RetrievalConfig
from memex_core.memory.confidence import MAX_VARIANCE, certainty
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryUnit
from memex_core.metrics import CONFIDENCE_BOOST_OBSERVED, CONFIDENCE_VARIANCE_OBSERVED


def _histogram_sum(histogram) -> float:
    """Read the cumulative sum of a Prometheus histogram (single source of truth).

    Hermes round-14 LOW: ``_sum`` is a private implementation detail of the
    Prometheus client. Wrap the access in one helper so a future
    ``prometheus_client`` upgrade that renames or removes the attribute
    needs to be updated in exactly one place across this test module.
    """
    return histogram._sum.get()


def _make_unit(
    confidence: float = 1.0,
    evidence_count: int = 0,
    text: str = 'fact',
    event_date: datetime | None = None,
) -> MemoryUnit:
    return MemoryUnit(
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
        confidence_evidence_count=evidence_count,
    )


def _make_engine(
    scores: list[float],
    *,
    confidence_alpha: float = 0.0,
    certainty_modulation_enabled: bool = False,
) -> RetrievalEngine:
    reranker = MagicMock()
    reranker.score.return_value = scores
    config = RetrievalConfig(
        reranking_recency_alpha=0.0,
        reranking_temporal_alpha=0.0,
        reranking_mw_alpha=0.0,
        confidence_alpha=confidence_alpha,
        certainty_modulation_enabled=certainty_modulation_enabled,
    )
    return RetrievalEngine(
        embedder=MagicMock(),
        reranker=reranker,
        retrieval_config=config,
    )


class TestShipDefaultBitForBitWithF47:
    """certainty_modulation_enabled = False (ship default) → F47 path unchanged.

    Pins the regression guarantee: a unit with non-zero evidence_count must
    produce the same boost factor as the pre-F22 F47 formula at the ship
    default. Asserted via the CONFIDENCE_BOOST_OBSERVED histogram delta.
    """

    @pytest.mark.asyncio
    async def test_default_flag_is_false(self) -> None:
        config = RetrievalConfig()
        assert config.certainty_modulation_enabled is False

    @pytest.mark.asyncio
    async def test_ship_default_ignores_evidence_count(self) -> None:
        """At ship default, two units with same confidence but different evidence_count
        produce identical boost factors (F47 form, no certainty multiplier)."""
        unit_low_evidence = _make_unit(confidence=0.85, evidence_count=0)
        unit_high_evidence = _make_unit(confidence=0.85, evidence_count=100)

        engine = _make_engine([0.0], confidence_alpha=0.3, certainty_modulation_enabled=False)

        sum_before_a = _histogram_sum(CONFIDENCE_BOOST_OBSERVED)
        await engine._rerank_results('q', [unit_low_evidence])
        boost_a = _histogram_sum(CONFIDENCE_BOOST_OBSERVED) - sum_before_a

        sum_before_b = _histogram_sum(CONFIDENCE_BOOST_OBSERVED)
        await engine._rerank_results('q', [unit_high_evidence])
        boost_b = _histogram_sum(CONFIDENCE_BOOST_OBSERVED) - sum_before_b

        # F47 baseline: 1.0 + 0.3 × (0.85 − 0.5) = 1.105
        expected = 1.0 + 0.3 * (0.85 - 0.5)
        assert math.isclose(boost_a, expected, rel_tol=1e-6)
        assert math.isclose(boost_b, expected, rel_tol=1e-6)


class TestColdStartNeutralWhenEnabled:
    """count=0 + certainty_modulation_enabled=True → boost collapses to neutral.

    Cold-start safety preserved. Beta(1, 1) prior → variance = MAX_VARIANCE
    → certainty = 0 → boost = 1.0 + α × Δ × 0 = 1.0.
    """

    @pytest.mark.asyncio
    async def test_cold_start_boost_is_neutral(self) -> None:
        unit = _make_unit(confidence=1.0, evidence_count=0)
        engine = _make_engine([0.0], confidence_alpha=0.3, certainty_modulation_enabled=True)
        sum_before = _histogram_sum(CONFIDENCE_BOOST_OBSERVED)
        await engine._rerank_results('q', [unit])
        boost = _histogram_sum(CONFIDENCE_BOOST_OBSERVED) - sum_before
        assert math.isclose(boost, 1.0, rel_tol=1e-6)


class TestVarianceMetricEmits:
    """When the flag is on, CONFIDENCE_VARIANCE_OBSERVED populates per unit."""

    @pytest.mark.asyncio
    async def test_variance_histogram_observes(self) -> None:
        unit = _make_unit(confidence=0.3, evidence_count=20)
        engine = _make_engine([0.0], confidence_alpha=0.3, certainty_modulation_enabled=True)
        sum_before = _histogram_sum(CONFIDENCE_VARIANCE_OBSERVED)
        await engine._rerank_results('q', [unit])
        sum_after = _histogram_sum(CONFIDENCE_VARIANCE_OBSERVED)
        # variance for (0.3, 20) ≈ 0.00943
        assert math.isclose(sum_after - sum_before, 105.0 / 11132.0, rel_tol=1e-6)

    @pytest.mark.asyncio
    async def test_variance_metric_emits_even_when_flag_off(self) -> None:
        """Hermes round-8 MED: the histogram MUST fire even with the flag
        off so operators have calibration data BEFORE flipping it. The
        prior gating-on-flag behaviour left them blind to the variance
        distribution they were about to enable scoring against.
        """
        unit = _make_unit(confidence=0.3, evidence_count=20)
        engine = _make_engine([0.0], confidence_alpha=0.3, certainty_modulation_enabled=False)
        sum_before = _histogram_sum(CONFIDENCE_VARIANCE_OBSERVED)
        await engine._rerank_results('q', [unit])
        sum_after = _histogram_sum(CONFIDENCE_VARIANCE_OBSERVED)
        # variance for (0.3, 20) ≈ 0.00943 — same emission path as the
        # flag-on case, just without the certainty multiplier on the boost.
        assert math.isclose(sum_after - sum_before, 105.0 / 11132.0, rel_tol=1e-6)


class TestModulatedBoostShape:
    """Per BACKLOG: well-evidenced contradicted unit gets full F47-shape penalty."""

    @pytest.mark.asyncio
    async def test_well_evidenced_contradicted_full_penalty(self) -> None:
        """confidence=0.3, count=20: variance ≈ 0.00943, certainty ≈ 0.887.

        Boost ≈ 1.0 + 0.3 × (0.3 − 0.5) × 0.887 ≈ 1.0 − 0.0532 = 0.947.
        """
        unit = _make_unit(confidence=0.3, evidence_count=20)
        engine = _make_engine([0.0], confidence_alpha=0.3, certainty_modulation_enabled=True)
        sum_before = _histogram_sum(CONFIDENCE_BOOST_OBSERVED)
        await engine._rerank_results('q', [unit])
        boost = _histogram_sum(CONFIDENCE_BOOST_OBSERVED) - sum_before
        c = certainty(0.3, 20)
        expected = 1.0 + 0.3 * (0.3 - 0.5) * c
        assert math.isclose(boost, expected, rel_tol=1e-6)

    @pytest.mark.asyncio
    async def test_well_evidenced_clean_full_lift(self) -> None:
        """confidence=1.0, count=20: variance ≈ 0.00189, certainty ≈ 0.977.

        Boost ≈ 1.0 + 0.3 × 0.5 × 0.977 ≈ 1.146.
        """
        unit = _make_unit(confidence=1.0, evidence_count=20)
        engine = _make_engine([0.0], confidence_alpha=0.3, certainty_modulation_enabled=True)
        sum_before = _histogram_sum(CONFIDENCE_BOOST_OBSERVED)
        await engine._rerank_results('q', [unit])
        boost = _histogram_sum(CONFIDENCE_BOOST_OBSERVED) - sum_before
        c = certainty(1.0, 20)
        expected = 1.0 + 0.3 * 0.5 * c
        assert math.isclose(boost, expected, rel_tol=1e-6)

    @pytest.mark.asyncio
    async def test_well_evidenced_midpoint_neutral(self) -> None:
        """confidence=0.5, count=20: (confidence − 0.5) = 0 → boost = 1.0 regardless of certainty."""
        unit = _make_unit(confidence=0.5, evidence_count=20)
        engine = _make_engine([0.0], confidence_alpha=0.3, certainty_modulation_enabled=True)
        sum_before = _histogram_sum(CONFIDENCE_BOOST_OBSERVED)
        await engine._rerank_results('q', [unit])
        boost = _histogram_sum(CONFIDENCE_BOOST_OBSERVED) - sum_before
        assert math.isclose(boost, 1.0, rel_tol=1e-6)


class TestMaxVariancePinning:
    """The certainty formula uses MAX_VARIANCE = 1/12 — pin via the engine."""

    def test_max_variance_pinned(self) -> None:
        assert MAX_VARIANCE == 1.0 / 12.0
