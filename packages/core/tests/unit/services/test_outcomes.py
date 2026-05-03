"""Unit tests for OutcomeService (MW outcome recording + score computation).

Pure-function tests for compute_mw_score and compute_mw_boost.
Integration tests for record_outcome live in tests/integration/.
"""

from datetime import datetime, timezone, timedelta

from memex_core.memory.sql_models import MWMode
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
        boost = compute_mw_boost(0, 100)
        assert boost > 0.0

    def test_equal_success_failure_gives_neutral(self):
        boost = compute_mw_boost(50, 50)
        assert abs(boost - 1.0) < 0.01

    def test_boost_formula_additive_marginal(self):
        success, failure = 8, 2
        score = compute_mw_score(success, failure)
        boost = compute_mw_boost(success, failure)
        assert abs(boost - (1.0 + MW_ALPHA_DEFAULT * (score - 0.5))) < 1e-10


class TestMwBoostEmaMode:
    """compute_mw_boost branches on vault mw_mode."""

    def test_ema_cold_start_boost_is_neutral(self):
        now = datetime.now(timezone.utc)
        boost = compute_mw_boost(
            0, 0, mw_mode=MWMode.EMA, last_outcome_at=None, half_life_days=60, now=now
        )
        assert boost == 1.0

    def test_stationary_default_matches_existing(self):
        boost_default = compute_mw_boost(9, 1)
        boost_explicit = compute_mw_boost(9, 1, mw_mode=MWMode.STATIONARY)
        assert boost_default == boost_explicit

    def test_ema_fresh_outcome_approximates_stationary(self):
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)
        boost_ema = compute_mw_boost(
            9, 1, mw_mode=MWMode.EMA, last_outcome_at=recent, half_life_days=60, now=now
        )
        boost_stationary = compute_mw_boost(9, 1, mw_mode=MWMode.STATIONARY)
        assert abs(boost_ema - boost_stationary) < 0.001

    def test_ema_old_outcome_decays_toward_neutral(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=90)
        boost = compute_mw_boost(
            9, 1, mw_mode=MWMode.EMA, last_outcome_at=old, half_life_days=60, now=now
        )
        assert 1.0 < boost < compute_mw_boost(9, 1, mw_mode=MWMode.STATIONARY)

    def test_ema_branches_on_enum_not_string(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=90)
        boost_enum = compute_mw_boost(
            9, 1, mw_mode=MWMode.EMA, last_outcome_at=old, half_life_days=60, now=now
        )
        boost_str = compute_mw_boost(
            9, 1, mw_mode='ema', last_outcome_at=old, half_life_days=60, now=now
        )
        assert boost_enum == boost_str
