"""Unit tests for EMA-decayed MW score computation."""

from datetime import datetime, timezone, timedelta

from memex_core.memory.retrieval.mw_ema import compute_mw_ema_score


class TestMwEmaScoreColdStart:
    def test_cold_start_returns_prior_mean(self):
        assert compute_mw_ema_score(0, 0, None, 60, datetime.now(timezone.utc)) == 0.5

    def test_cold_start_with_counters_returns_prior_mean(self):
        assert compute_mw_ema_score(10, 5, None, 60, datetime.now(timezone.utc)) == 0.5

    def test_cold_start_identical_to_stationary(self):
        from memex_core.services.outcomes import compute_mw_score

        assert compute_mw_ema_score(0, 0, None, 60, datetime.now(timezone.utc)) == compute_mw_score(
            0, 0
        )


class TestMwEmaScoreDecay:
    def test_fresh_outcome_no_decay(self):
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)
        score = compute_mw_ema_score(10, 0, recent, 60, now)
        assert score > 0.9

    def test_decay_shifts_toward_prior_mean(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=90)
        decayed = compute_mw_ema_score(10, 0, old, 60, now)
        fresh = compute_mw_ema_score(10, 0, now - timedelta(hours=1), 60, now)
        assert abs(decayed - 0.5) < abs(fresh - 0.5)

    def test_90_day_decay_with_60_day_half_life(self):
        import math

        now = datetime.now(timezone.utc)
        old = now - timedelta(days=90)
        score = compute_mw_ema_score(10, 0, old, 60, now)
        decay = math.exp(-90 * math.log(2) / 60)
        expected = (10 * decay + 1.0) / (10 * decay + 0 * decay + 2.0)
        assert abs(score - expected) < 1e-12

    def test_60_day_half_life_exact(self):
        now = datetime.now(timezone.utc)
        exactly_one_half_life = now - timedelta(days=60)
        score = compute_mw_ema_score(10, 0, exactly_one_half_life, 60, now)
        assert abs(score - (5 + 1) / (5 + 2)) < 1e-10

    def test_very_old_approaches_prior_mean(self):
        now = datetime.now(timezone.utc)
        ancient = now - timedelta(days=3650)
        score = compute_mw_ema_score(100, 0, ancient, 60, now)
        assert abs(score - 0.5) < 0.01

    def test_negative_elapsed_treated_as_zero(self):
        now = datetime.now(timezone.utc)
        future = now + timedelta(hours=1)
        score = compute_mw_ema_score(10, 0, future, 60, now)
        no_decay = compute_mw_ema_score(10, 0, now, 60, now)
        assert abs(score - no_decay) < 1e-10


class TestMwEmaScoreSymmetry:
    def test_equal_decayed_counts_converge_to_prior(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=90)
        score_high = compute_mw_ema_score(10, 0, old, 60, now)
        score_low = compute_mw_ema_score(0, 10, old, 60, now)
        assert abs(score_high - (1 - score_low)) < 1e-12


class TestMwEmaScoreFormula:
    def test_matches_closed_form(self):
        import math

        now = datetime.now(timezone.utc)
        last = now - timedelta(days=45)
        success, failure = 7, 3
        half_life = 60
        elapsed_days = 45.0
        decay = math.exp(-elapsed_days * math.log(2) / half_life)
        sd = success * decay
        fd = failure * decay
        expected = (sd + 1.0) / (sd + fd + 2.0)
        actual = compute_mw_ema_score(success, failure, last, half_life, now)
        assert abs(actual - expected) < 1e-12
