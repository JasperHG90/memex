"""F22 — Beta(1, 1) posterior helpers in ``memex_core.memory.confidence``.

Pins the five worked numerical cases from BACKLOG.md F22 so any change to
the prior or the closed-form arithmetic is a deliberate edit.
"""

from __future__ import annotations

import math

import pytest

from memex_core.memory.confidence import (
    MAX_VARIANCE,
    certainty,
    mean_and_variance,
    sample_concentration,
)


REL_TOL = 1e-6


class TestPriorPin:
    """The Beta(1, 1) uniform prior is load-bearing — pin its variance."""

    def test_max_variance_is_one_twelfth_exactly(self) -> None:
        """``MAX_VARIANCE = 1/12`` — variance of Uniform(0, 1) = Beta(1, 1).

        A future prior change MUST be a deliberate edit; this test forces
        the migration to fail loud.
        """
        assert MAX_VARIANCE == 1.0 / 12.0


class TestColdStart:
    """count=0 → posterior collapses to Beta(1, 1) prior."""

    def test_cold_start_variance_equals_max(self) -> None:
        """At evidence_count=0, variance = MAX_VARIANCE exactly."""
        _, variance = mean_and_variance(1.0, 0)
        assert variance == MAX_VARIANCE

    def test_cold_start_certainty_is_zero(self) -> None:
        assert certainty(1.0, 0) == 0.0

    def test_cold_start_at_arbitrary_confidence(self) -> None:
        """Variance at count=0 is the prior variance regardless of mean.

        The posterior parameters are ``(1 + c·0, 1 + (1-c)·0) = (1, 1)`` —
        the Beta(1, 1) prior, independent of the supplied confidence.
        """
        for c in (0.0, 0.3, 0.5, 0.85, 1.0):
            _, variance = mean_and_variance(c, 0)
            assert variance == MAX_VARIANCE
            assert certainty(c, 0) == 0.0


class TestSingleObservationMaxConfidence:
    """confidence=1.0, count=1 → Beta(2, 1)."""

    def test_variance(self) -> None:
        _, variance = mean_and_variance(1.0, 1)
        # variance = 2 / (3² × 4) = 2/36 ≈ 0.0556
        assert math.isclose(variance, 2.0 / 36.0, rel_tol=REL_TOL)

    def test_certainty_partial(self) -> None:
        # certainty = 1 - (2/36) / (1/12) = 1 - 24/36 = 12/36 = 1/3 ≈ 0.333
        assert math.isclose(certainty(1.0, 1), 1.0 / 3.0, rel_tol=REL_TOL)


class TestWellEvidencedContradicted:
    """confidence=0.3, count=20 → Beta(7, 15)."""

    def test_posterior_parameters(self) -> None:
        # α = 1 + 0.3 × 20 = 7, β = 1 + 0.7 × 20 = 15
        # variance = 7 × 15 / (22² × 23) = 105 / 11132
        _, variance = mean_and_variance(0.3, 20)
        expected = 105.0 / 11132.0
        assert math.isclose(variance, expected, rel_tol=REL_TOL)
        assert math.isclose(variance, 0.00943, abs_tol=1e-5)

    def test_certainty_high(self) -> None:
        # certainty = 1 - 0.00943 / (1/12) ≈ 1 - 0.1132 ≈ 0.887
        c = certainty(0.3, 20)
        assert math.isclose(c, 1.0 - (105.0 / 11132.0) / MAX_VARIANCE, rel_tol=REL_TOL)
        assert math.isclose(c, 0.887, abs_tol=1e-3)


class TestWellEvidencedMidpoint:
    """confidence=0.5, count=20 → Beta(11, 11) — REGRESSION GUARD.

    Even with high certainty, the boost is neutral because (confidence − 0.5) = 0.
    "We are confident the truth is genuinely uncertain" — F47's neutral-by-construction case.
    """

    def test_variance(self) -> None:
        _, variance = mean_and_variance(0.5, 20)
        # variance = 11 × 11 / (22² × 23) = 121 / 11132
        expected = 121.0 / 11132.0
        assert math.isclose(variance, expected, rel_tol=REL_TOL)
        assert math.isclose(variance, 0.0109, abs_tol=1e-4)

    def test_certainty_high(self) -> None:
        assert math.isclose(certainty(0.5, 20), 0.87, abs_tol=1e-2)

    def test_boost_is_neutral_by_formula(self) -> None:
        """Even with α=0.3 and certainty≈0.87, boost = 1.0 because (0.5 − 0.5) = 0."""
        confidence = 0.5
        evidence_count = 20
        confidence_alpha = 0.3
        c = certainty(confidence, evidence_count)
        boost = 1.0 + confidence_alpha * (confidence - 0.5) * c
        assert math.isclose(boost, 1.0, rel_tol=REL_TOL)


class TestWellEvidencedClean:
    """confidence=1.0, count=20 → Beta(21, 1)."""

    def test_variance(self) -> None:
        _, variance = mean_and_variance(1.0, 20)
        # variance = 21 × 1 / (22² × 23) = 21 / 11132
        expected = 21.0 / 11132.0
        assert math.isclose(variance, expected, rel_tol=REL_TOL)
        assert math.isclose(variance, 0.00189, abs_tol=1e-4)

    def test_certainty_near_one(self) -> None:
        c = certainty(1.0, 20)
        assert math.isclose(c, 0.977, abs_tol=1e-3)

    def test_boost_near_full_lift(self) -> None:
        """High-evidence clean unit gets near-full F47 lift."""
        confidence = 1.0
        evidence_count = 20
        confidence_alpha = 0.3
        c = certainty(confidence, evidence_count)
        boost = 1.0 + confidence_alpha * (confidence - 0.5) * c
        # Full F47 boost at certainty=1.0 would be 1.15; at c≈0.977 we get ≈1.146
        assert math.isclose(boost, 1.0 + 0.3 * 0.5 * c, rel_tol=REL_TOL)
        assert 1.14 < boost < 1.15


class TestSampleConcentration:
    def test_known_pair(self) -> None:
        assert sample_concentration(7.0, 15.0) == 22.0

    def test_zero_pair(self) -> None:
        assert sample_concentration(0.0, 0.0) == 0.0


class TestMeanIsInputConfidence:
    """The first tuple element is the *input* confidence — NOT the Beta-posterior mean.

    F22 uses the stored ``confidence`` as the point estimate for downstream
    scoring (see ``confidence.py`` module docstring). The true Beta-posterior
    mean ``(1 + c·n) / (2 + n)`` is intentionally NOT returned here so the
    F47 boost stays anchored on the row's stored confidence value rather
    than drifting toward 0.5 as evidence accumulates with c ≠ 0.5.
    """

    @pytest.mark.parametrize(
        ('confidence', 'count'),
        [(0.0, 0), (1.0, 0), (0.5, 5), (0.3, 20), (0.85, 12)],
    )
    def test_mean_passthrough(self, confidence: float, count: int) -> None:
        m, _ = mean_and_variance(confidence, count)
        assert m == confidence


class TestVarianceMonotonicity:
    """Variance shrinks as evidence accumulates (for a given mean)."""

    def test_more_evidence_lowers_variance(self) -> None:
        confidence = 0.7
        prev = float('inf')
        for count in (0, 1, 5, 10, 50, 100):
            _, variance = mean_and_variance(confidence, count)
            assert variance < prev, f'count={count}: variance {variance} not < {prev}'
            prev = variance

    def test_variance_is_bounded_above_by_max(self) -> None:
        for count in range(0, 50, 5):
            for confidence in (0.0, 0.3, 0.5, 0.85, 1.0):
                _, variance = mean_and_variance(confidence, count)
                assert variance <= MAX_VARIANCE + 1e-12


class TestDtoFormulaConsistency:
    """Pin: schemas.MemoryUnitDTO derives variance via the same formula.

    The formula is duplicated in ``MemoryUnitDTO._compute_confidence_variance``
    to avoid memex_common depending on memex_core. This test guards that
    duplication against drift.
    """

    @pytest.mark.parametrize(
        ('confidence', 'count'),
        [(1.0, 0), (1.0, 1), (0.3, 20), (0.5, 20), (1.0, 20), (0.85, 12), (0.0, 5)],
    )
    def test_dto_matches_helper(self, confidence: float, count: int) -> None:
        from uuid import uuid4

        from memex_common.schemas import MemoryUnitDTO
        from memex_common.types import FactTypes

        dto = MemoryUnitDTO(
            id=uuid4(),
            text='x',
            fact_type=FactTypes.WORLD,
            vault_id=uuid4(),
            confidence=confidence,
            confidence_evidence_count=count,
        )
        _, expected = mean_and_variance(confidence, count)
        assert math.isclose(dto.confidence_variance, expected, rel_tol=REL_TOL)
