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
    extract_confidence_and_count,
    mean_and_variance,
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

    def test_dto_max_variance_constant_matches_core(self) -> None:
        """Hermes round-20 LOW: the schemas.py ``_MAX_VARIANCE`` constant
        used by ``MemoryUnitDTO.confidence_variance``'s ``Field(le=…)``
        constraint and validator bounds check MUST equal the core
        ``MAX_VARIANCE`` constant. The two are deliberately duplicated
        because ``memex_common`` cannot depend on ``memex_core``; this
        test pins their equivalence per release.
        """
        from memex_common.schemas import _MAX_VARIANCE

        assert _MAX_VARIANCE == MAX_VARIANCE


class TestExtractConfidenceAndCount:
    """``extract_confidence_and_count`` — single source of truth for the
    defensive ``(confidence, evidence_count)`` extraction (Hermes round-4 MED).

    Replaces three previously-duplicated copies of the pattern in
    ``engine.py``, ``reflection.py``, and ``exploration.py``.
    """

    def test_normal_attributes_pass_through(self):
        class _U:
            confidence = 0.5
            confidence_evidence_count = 7

        assert extract_confidence_and_count(_U()) == (0.5, 7)

    def test_zero_confidence_preserved_not_coerced(self):
        """``0.0`` is a legitimate value — must NOT be silently coerced to 1.0."""

        class _U:
            confidence = 0.0
            confidence_evidence_count = 5

        assert extract_confidence_and_count(_U()) == (0.0, 5)

    def test_none_confidence_falls_back_to_one(self):
        class _U:
            confidence = None
            confidence_evidence_count = 5

        c, n = extract_confidence_and_count(_U())
        assert c == 1.0
        assert n == 5

    def test_none_evidence_count_falls_back_to_zero(self):
        class _U:
            confidence = 0.5
            confidence_evidence_count = None

        c, n = extract_confidence_and_count(_U())
        assert c == 0.5
        assert n == 0

    def test_missing_attributes_use_defaults(self):
        class _U:
            pass

        c, n = extract_confidence_and_count(_U())
        assert c == 1.0
        assert n == 0

    def test_int_confidence_coerced_to_float(self):
        class _U:
            confidence = 1
            confidence_evidence_count = 3

        c, n = extract_confidence_and_count(_U())
        assert isinstance(c, float)
        assert c == 1.0
        assert n == 3

    def test_float_evidence_count_rounds_to_nearest_int(self):
        """Hermes round-20 MED: float evidence counts are rounded, not truncated.

        ``int(2.9) = 2`` silently drops nearly a full evidence event; the
        helper now uses ``round()`` so a mid-pipeline float snaps to the
        nearest integer. This pins the rejection of ``int()`` semantics
        and the acceptance of ``round()`` semantics.
        """

        class _U:
            confidence = 0.5
            confidence_evidence_count = 2.9  # weird but legal mid-pipeline

        c, n = extract_confidence_and_count(_U())
        # Reject ``int(2.9) = 2`` semantics — that's the bug.
        assert n != 2
        # Accept ``round(2.9) = 3`` semantics — that's the fix.
        assert n == 3
        assert isinstance(n, int)

    def test_float_evidence_count_rounds_down_for_below_half(self):
        """Symmetric pin: ``2.4`` rounds down to ``2``."""

        class _U:
            confidence = 0.5
            confidence_evidence_count = 2.4

        _, n = extract_confidence_and_count(_U())
        assert n == 2
        assert isinstance(n, int)

    def test_float_evidence_count_uses_half_up_not_bankers_rounding(self):
        """Hermes round-21 MED: half-integer evidence counts round half-up,
        not via Python's banker's rounding.

        ``round(2.5) == 2`` in Python (banker's rounding to even), which
        would silently drop half an evidence event. The helper uses
        ``math.floor(x + 0.5)`` so ``2.5 → 3`` and ``3.5 → 4`` —
        intuitive half-up semantics for a counter.
        """

        class _U25:
            confidence = 0.5
            confidence_evidence_count = 2.5

        class _U35:
            confidence = 0.5
            confidence_evidence_count = 3.5

        _, n_25 = extract_confidence_and_count(_U25())
        _, n_35 = extract_confidence_and_count(_U35())
        # Reject banker's rounding: ``round(2.5) = 2``, ``round(3.5) = 4``.
        # Accept half-up: both round upward at the half.
        assert n_25 == 3
        assert n_35 == 4

    def test_out_of_range_confidence_clamped_not_raised(self):
        """Hermes round-8 HIGH: an in-flight ``confidence > 1.0`` from a
        concurrent write must NOT propagate to ``mean_and_variance`` (which
        would raise) — it must be clamped here so the retrieval rerank
        path stays available even with stale/in-flight model objects.
        """

        class _U:
            confidence = 1.0001  # out-of-range, mirrors a concurrent-write race.
            confidence_evidence_count = 5

        c, n = extract_confidence_and_count(_U())
        assert c == 1.0
        # And the downstream call MUST not raise — proves the clamp closes
        # the round-8 HIGH crash path.
        m, v = mean_and_variance(c, n)
        assert m == 1.0
        assert 0.0 <= v <= MAX_VARIANCE

    def test_negative_confidence_clamped_not_raised(self):
        class _U:
            confidence = -0.5
            confidence_evidence_count = 5

        c, n = extract_confidence_and_count(_U())
        assert c == 0.0
        # Same crash-avoidance invariant.
        m, v = mean_and_variance(c, n)
        assert m == 0.0
        assert 0.0 <= v <= MAX_VARIANCE

    def test_negative_evidence_count_floored_at_zero(self):
        class _U:
            confidence = 0.5
            confidence_evidence_count = -3

        c, n = extract_confidence_and_count(_U())
        assert n == 0
        m, v = mean_and_variance(c, n)
        assert m == 0.5
        # Cold-start variance because count was floored to 0.
        assert math.isclose(v, MAX_VARIANCE, rel_tol=REL_TOL)


class TestMeanAndVarianceInputValidation:
    """``mean_and_variance`` rejects out-of-range inputs (Hermes round-6 MED).

    The Beta(1, 1) shape parameters go negative when ``confidence`` is
    outside ``[0, 1]`` — the helper now surfaces the precondition violation
    at the computation site rather than returning silent garbage.
    """

    @pytest.mark.parametrize('bad_confidence', [-0.001, -1.0, 1.0001, 2.0, float('nan')])
    def test_confidence_out_of_range_raises(self, bad_confidence: float) -> None:
        with pytest.raises(ValueError, match='confidence must be in '):
            mean_and_variance(bad_confidence, 0)

    def test_negative_evidence_count_raises(self) -> None:
        with pytest.raises(ValueError, match='evidence_count must be non-negative'):
            mean_and_variance(0.5, -1)

    @pytest.mark.parametrize('boundary', [0.0, 1.0])
    def test_boundary_confidence_values_accepted(self, boundary: float) -> None:
        """0 and 1 are valid (the unit-interval endpoints)."""
        m, v = mean_and_variance(boundary, 5)
        assert m == boundary
        assert 0.0 <= v <= MAX_VARIANCE
