"""Bounds on ``ContradictionConfig`` fields.

These constraints exist so the contradict penalty
``1 - superseded_threshold + alpha`` produces a meaningful confidence drop
without trivially clamping to 0 or staying above the threshold.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memex_common.config import ContradictionConfig


class TestContradictionConfigBounds:
    @pytest.mark.parametrize('bad_alpha', [0.0, -0.1, 1.1, 2.0])
    def test_alpha_rejected_outside_open_zero_one(self, bad_alpha: float) -> None:
        with pytest.raises(ValidationError):
            ContradictionConfig(alpha=bad_alpha)

    def test_alpha_one_is_accepted(self) -> None:
        cfg = ContradictionConfig(alpha=1.0)
        assert cfg.alpha == 1.0

    @pytest.mark.parametrize('bad_threshold', [0.0, 1.0, -0.1, 1.1])
    def test_superseded_threshold_rejected_at_or_outside_unit_interval(
        self, bad_threshold: float
    ) -> None:
        with pytest.raises(ValidationError):
            ContradictionConfig(superseded_threshold=bad_threshold)

    @pytest.mark.parametrize('good_threshold', [0.01, 0.3, 0.5, 0.99])
    def test_superseded_threshold_accepts_strict_interior(self, good_threshold: float) -> None:
        cfg = ContradictionConfig(superseded_threshold=good_threshold)
        assert cfg.superseded_threshold == good_threshold

    @pytest.mark.parametrize('bad_sim', [-0.01, 1.01, 2.0, -1.0])
    def test_similarity_threshold_clamped_to_unit_interval(self, bad_sim: float) -> None:
        with pytest.raises(ValidationError):
            ContradictionConfig(similarity_threshold=bad_sim)

    @pytest.mark.parametrize('bad_k', [0, -1])
    def test_max_candidates_per_unit_must_be_positive(self, bad_k: int) -> None:
        with pytest.raises(ValidationError):
            ContradictionConfig(max_candidates_per_unit=bad_k)


class TestPenaltyMathInvariant:
    """For every accepted (alpha, superseded_threshold) combo, the contradict
    penalty must drop a fresh unit (confidence=1.0) strictly below the
    threshold AND the resulting confidence must clamp into [0, 1].
    """

    @pytest.mark.parametrize(
        'alpha,threshold',
        [
            (0.1, 0.3),  # defaults
            (0.05, 0.1),
            (0.5, 0.99),
            (1.0, 0.01),
            (0.01, 0.5),
        ],
    )
    def test_penalty_drops_below_threshold(self, alpha: float, threshold: float) -> None:
        cfg = ContradictionConfig(alpha=alpha, superseded_threshold=threshold)
        penalty = 1.0 - cfg.superseded_threshold + cfg.alpha
        final = max(0.0, min(1.0, 1.0 - penalty))
        assert final < cfg.superseded_threshold, (
            f'penalty={penalty} leaves final={final}, threshold={threshold}'
        )
