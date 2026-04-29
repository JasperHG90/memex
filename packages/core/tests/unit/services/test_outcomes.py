"""Unit tests for OutcomeService (MW outcome recording + score computation).

Pure-function tests for compute_mw_score and compute_mw_boost.
Integration tests for record_outcome live in tests/integration/.
"""

from memex_core.services.outcomes import (
    compute_mw_boost,
    compute_mw_score,
    MW_ALPHA_DEFAULT,
)


class TestMwScoreComputation:
    """Beta-Bernoulli posterior mean with α=β=1 uniform prior."""

    def test_cold_start_is_neutral(self):
        assert compute_mw_score(0, 0) == 0.5

    def test_all_success(self):
        assert compute_mw_score(10, 0) == (10 + 1) / (10 + 0 + 2)

    def test_all_failure(self):
        assert compute_mw_score(0, 10) == (0 + 1) / (0 + 10 + 2)

    def test_mixed_signals(self):
        score = compute_mw_score(7, 3)
        assert score == (7 + 1) / (7 + 3 + 2)
        assert 0.5 < score < 1.0

    def test_symmetry(self):
        # (3+1)/(3+7+2) vs (7+1)/(7+3+2)
        low = compute_mw_score(3, 7)
        high = compute_mw_score(7, 3)
        assert low < 0.5 < high


class TestMwBoostComputation:
    """Additive-marginal boost factor for retrieval composition."""

    def test_cold_start_boost_is_neutral(self):
        assert compute_mw_boost(0, 0) == 1.0

    def test_high_success_boosts(self):
        boost = compute_mw_boost(9, 1)
        assert boost > 1.0
        expected = 1.0 + MW_ALPHA_DEFAULT * (compute_mw_score(9, 1) - 0.5)
        assert abs(boost - expected) < 1e-10

    def test_high_failure_downweights(self):
        boost = compute_mw_boost(1, 9)
        assert boost < 1.0
        expected = 1.0 + MW_ALPHA_DEFAULT * (compute_mw_score(1, 9) - 0.5)
        assert abs(boost - expected) < 1e-10

    def test_custom_alpha(self):
        boost_high = compute_mw_boost(9, 1, mw_alpha=1.0)
        boost_default = compute_mw_boost(9, 1, mw_alpha=MW_ALPHA_DEFAULT)
        assert boost_high > boost_default

    def test_boost_never_zeros(self):
        # Even 0 successes and 100 failures, boost is still positive
        boost = compute_mw_boost(0, 100)
        assert boost > 0.0  # additive-marginal guarantees > 0

    def test_equal_success_failure_gives_neutral(self):
        # With large equal counts, score converges to 0.5, boost to 1.0
        boost = compute_mw_boost(50, 50)
        assert abs(boost - 1.0) < 0.01

    def test_boost_formula_additive_marginal(self):
        # Verify: mw_boost = 1.0 + alpha * (score - 0.5)
        success, failure = 8, 2
        score = compute_mw_score(success, failure)
        boost = compute_mw_boost(success, failure)
        assert abs(boost - (1.0 + MW_ALPHA_DEFAULT * (score - 0.5))) < 1e-10
