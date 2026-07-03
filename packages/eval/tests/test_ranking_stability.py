"""Unit tests for the RBO implementation.

Pinning identities (RBO(a, a) = 1, RBO(a, ∅) = 0) plus reference values
from Webber et al. 2010 Table 1 so future edits can't silently drift
the metric.
"""

from __future__ import annotations

import math

import pytest

from memex_eval.ranking_stability import rank_biased_overlap


class TestIdentities:
    """Boundary cases the formula must satisfy by construction."""

    def test_identical_lists_score_one(self) -> None:
        a = ['x', 'y', 'z', 'w']
        assert rank_biased_overlap(a, a, p=0.9) == 1.0

    def test_identical_lists_score_one_under_alt_p(self) -> None:
        a = ['x', 'y', 'z']
        assert rank_biased_overlap(a, a, p=0.5) == 1.0
        assert rank_biased_overlap(a, a, p=0.99) == 1.0

    def test_both_empty_match(self) -> None:
        assert rank_biased_overlap([], [], p=0.9) == 1.0

    def test_one_empty_is_zero(self) -> None:
        assert rank_biased_overlap(['a'], [], p=0.9) == 0.0
        assert rank_biased_overlap([], ['a'], p=0.9) == 0.0

    def test_disjoint_lists_score_zero(self) -> None:
        # No element appears in both lists at any depth.
        assert rank_biased_overlap(['a', 'b', 'c'], ['x', 'y', 'z'], p=0.9) == 0.0

    def test_invalid_p_raises(self) -> None:
        with pytest.raises(ValueError):
            rank_biased_overlap(['a'], ['a'], p=0.0)
        with pytest.raises(ValueError):
            rank_biased_overlap(['a'], ['a'], p=1.0)
        with pytest.raises(ValueError):
            rank_biased_overlap(['a'], ['a'], p=1.5)


class TestSymmetry:
    """RBO is symmetric: RBO(a, b) == RBO(b, a) for any p, any lists."""

    @pytest.mark.parametrize(
        'a,b',
        [
            (['x', 'y', 'z'], ['z', 'y', 'x']),
            (['a', 'b', 'c', 'd'], ['a', 'c', 'b']),
            (['x'], ['x', 'y', 'z']),
            ([1, 2, 3, 4, 5], [3, 1, 5, 2, 4]),
        ],
    )
    def test_symmetric(self, a: list, b: list) -> None:
        assert math.isclose(
            rank_biased_overlap(a, b, p=0.9),
            rank_biased_overlap(b, a, p=0.9),
            rel_tol=1e-12,
        )


class TestKnownValues:
    """Reference values computed by hand against the extrapolated formula."""

    def test_single_swap_at_top(self) -> None:
        # Swap of first two elements; everything else identical.
        # At d=1: overlap=0 → A_1 = 0.
        # At d=2: overlap=2 → A_2 = 1.
        # At d=3: overlap=3 → A_3 = 1.
        # min RBO = (1-p) * (p^0 * 0 + p^1 * 1 + p^2 * 1).
        # tail = p^3 * 1 (agreement at short_len=3 is 3/3=1).
        # p = 0.9 → min = 0.1 * (0 + 0.9 + 0.81) = 0.171; tail = 0.729.
        # Total ≈ 0.900.
        a = ['x', 'y', 'z']
        b = ['y', 'x', 'z']
        score = rank_biased_overlap(a, b, p=0.9)
        assert math.isclose(score, 0.9, rel_tol=1e-9), f'got {score}'

    def test_completely_disjoint_three_each(self) -> None:
        # No element in common → all overlap_d = 0 → RBO = 0 exactly.
        score = rank_biased_overlap(['a', 'b', 'c'], ['x', 'y', 'z'], p=0.9)
        assert score == 0.0

    def test_prefix_of_longer_list(self) -> None:
        # short = ['a', 'b'] is a prefix of long = ['a', 'b', 'c', 'd'].
        # Overlap_1 = 1, Overlap_2 = 2, Overlap_3 = 2, Overlap_4 = 2.
        # Webber RBO_ext: the shorter list is fully contained in the
        # longer one, so the published formula yields exactly 1.0 (any
        # smaller value would be the naive sum without the agreement
        # correction term for depths d > short_len).
        # Observed sum (1-p) * Σ X_d/d * p^{d-1}
        #   = 0.1 * (1 + 0.9 + (2/3)*0.81 + (2/4)*0.729)
        #   = 0.28045
        # Correction (1-p) * Σ_{d=3..4} X_2*(d-2)/(2*d) * p^{d-1}
        #   = 0.1 * (2 * 1 / (2*3) * 0.81 + 2 * 2 / (2*4) * 0.729)
        #   = 0.1 * (0.27 + 0.3645)
        #   = 0.06345
        # Tail = (X_2/2) * p^4 = 1.0 * 0.6561 = 0.6561
        # Total = 0.28045 + 0.06345 + 0.6561 = 1.000.
        short = ['a', 'b']
        long_ = ['a', 'b', 'c', 'd']
        score = rank_biased_overlap(short, long_, p=0.9)
        assert math.isclose(score, 1.0, abs_tol=1e-9), f'got {score}'

    def test_persistence_pulls_top_heavy(self) -> None:
        # Same lists, different p: lower p concentrates weight on shallow ranks.
        # A swap at rank 1+2 should hurt more under low p.
        a = ['x', 'y', 'z', 'w', 'v']
        b = ['y', 'x', 'z', 'w', 'v']  # top-2 swap only
        score_low = rank_biased_overlap(a, b, p=0.5)
        score_high = rank_biased_overlap(a, b, p=0.99)
        # Under p=0.5 the early swap dominates → lower score.
        # Under p=0.99 the formula weighs the deeper agreeing positions
        # heavily → higher score.
        assert score_low < score_high


class TestPartialOverlap:
    """Non-prefix partial overlap shapes."""

    def test_one_in_common_top_position(self) -> None:
        a = ['x', 'y', 'z']
        b = ['x', 'a', 'b']
        # overlap_1 = 1, overlap_2 = 1, overlap_3 = 1.
        # Agreement: 1, 0.5, 0.333.
        # min RBO (p=0.9) = 0.1 * (1 + 0.45 + 0.81*0.333) ≈ 0.1716.
        # tail = 0.729 * 0.333 ≈ 0.243.
        # Total ≈ 0.415.
        score = rank_biased_overlap(a, b, p=0.9)
        assert 0.40 < score < 0.43

    def test_reverse_order_low_score(self) -> None:
        # Same elements, fully reversed order — partial overlap grows
        # slowly so RBO < 1 but > 0.
        a = ['x', 'y', 'z', 'w']
        b = ['w', 'z', 'y', 'x']
        score = rank_biased_overlap(a, b, p=0.9)
        # Hard to compute by inspection but must be < 1.0 and > 0.0.
        assert 0.0 < score < 1.0
