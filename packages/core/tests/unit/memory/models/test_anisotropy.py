"""Unit tests for AnisotropyCorrector (D-MEM Z-score embedding anisotropy correction)."""

import math

from memex_core.memory.models.anisotropy import (
    AnisotropyCorrector,
    AnisotropyCorrectorGroup,
)


class TestAnisotropyCorrectorColdStart:
    """Cold-start: passthrough until min_samples reached."""

    def test_returns_raw_score_below_min_samples(self):
        c = AnisotropyCorrector(min_samples=5)
        # First 4 observations should pass through unchanged
        for i in range(4):
            result = c.normalize(0.85)
            assert result == 0.85

    def test_activates_at_min_samples(self):
        c = AnisotropyCorrector(min_samples=3)
        c.normalize(0.80)
        c.normalize(0.90)
        # Third observation triggers normalization
        result = c.normalize(0.85)
        assert c.count == 3
        # With some variance, normalization should produce a valid result
        assert 0.0 < result < 1.0

    def test_cold_start_returns_exactly_raw(self):
        c = AnisotropyCorrector(min_samples=100)
        raw = 0.73
        assert c.normalize(raw) == raw


class TestAnisotropyCorrectorNormalization:
    """Z-score → sigmoid normalization behavior."""

    def test_high_score_gets_high_normalized(self):
        c = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        # Feed a tight cluster of low scores
        for _ in range(10):
            c.normalize(0.70)
        # Now feed a genuinely high score
        result = c.normalize(0.90)
        # Should be well above 0.5 (sigmoid of positive z-score)
        assert result > 0.5

    def test_low_score_gets_low_normalized(self):
        c = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        # Feed a tight cluster of high scores
        for _ in range(10):
            c.normalize(0.90)
        # Now feed a genuinely low score
        result = c.normalize(0.70)
        # Should be well below 0.5 (sigmoid of negative z-score)
        assert result < 0.5

    def test_mean_score_gets_neutral(self):
        c = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        for _ in range(10):
            c.normalize(0.80)
        # A score right at the mean should give z≈0, sigmoid(0)=0.5
        result = c.normalize(0.80)
        assert abs(result - 0.5) < 0.05

    def test_sigmoid_output_bounded(self):
        c = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        # Feed some values then check extreme scores
        for v in [0.75, 0.76, 0.74, 0.77, 0.73]:
            c.normalize(v)
        # Very extreme scores should still be in (0, 1)
        high = c.normalize(0.99)
        low = c.normalize(0.01)
        assert 0.0 < low < 1.0
        assert 0.0 < high < 1.0
        assert high > low

    def test_output_increases_monotonically_with_input(self):
        c = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        # Seed with moderate values
        for v in [0.80, 0.82, 0.78, 0.81, 0.79]:
            c.normalize(v)
        # Higher input → higher output
        results = [c.normalize(v) for v in [0.70, 0.80, 0.90]]
        assert results[0] < results[1] < results[2]


class TestAnisotropyCorrectorSlidingWindow:
    """Sliding window statistics."""

    def test_window_size_limits_observations(self):
        c = AnisotropyCorrector(window_size=5, min_samples=2)
        for v in [0.80] * 100:
            c.normalize(v)
        # Window should be capped at 5
        assert c.count == 5

    def test_window_eviction_shifts_mean(self):
        c = AnisotropyCorrector(window_size=3, min_samples=2, epsilon=1e-8)
        # Fill window with low values
        c.normalize(0.70)
        c.normalize(0.70)
        # Now push high values to evict old ones
        c.normalize(0.90)
        # Mean should have shifted up
        assert c.mean > 0.75

    def test_reset_clears_state(self):
        c = AnisotropyCorrector(min_samples=2)
        for _ in range(10):
            c.normalize(0.85)
        assert c.count > 0
        c.reset()
        assert c.count == 0
        # After reset, should passthrough again
        assert c.normalize(0.50) == 0.50


class TestAnisotropyCorrectorRunningStats:
    """Verify running mean and std computation."""

    def test_running_mean(self):
        c = AnisotropyCorrector(min_samples=100)
        values = [0.8, 0.9, 0.7, 0.85, 0.75]
        for v in values:
            c.normalize(v)
        expected_mean = sum(values) / len(values)
        assert abs(c.mean - expected_mean) < 1e-10

    def test_running_std(self):
        c = AnisotropyCorrector(min_samples=100)
        values = [0.8, 0.9, 0.7, 0.85, 0.75]
        for v in values:
            c.normalize(v)
        mean = sum(values) / len(values)
        expected_var = sum((v - mean) ** 2 for v in values) / len(values)
        expected_std = math.sqrt(expected_var)
        assert abs(c.std - expected_std) < 1e-10

    def test_constant_input_std_zero(self):
        c = AnisotropyCorrector(min_samples=100)
        for _ in range(10):
            c.normalize(0.85)
        assert c.std == 0.0

    def test_constant_input_with_normalization_gives_neutral(self):
        """When all scores are identical, normalization returns 0.5 (neutral)."""
        c = AnisotropyCorrector(min_samples=3)
        c.normalize(0.85)
        c.normalize(0.85)
        result = c.normalize(0.85)
        # z_score = 0 / epsilon → 0, sigmoid(0) = 0.5
        assert abs(result - 0.5) < 0.01


class TestAnisotropyCorrectorGroup:
    """Named corrector group."""

    def test_get_returns_same_corrector(self):
        group = AnisotropyCorrectorGroup(names=['retrieval', 'contradiction'])
        r1 = group.get('retrieval')
        r2 = group.get('retrieval')
        assert r1 is r2

    def test_get_creates_on_demand(self):
        group = AnisotropyCorrectorGroup()
        c = group.get('dedup')
        assert isinstance(c, AnisotropyCorrector)

    def test_reset_clears_all(self):
        group = AnisotropyCorrectorGroup(names=['a', 'b'])
        for _ in range(10):
            group.get('a').normalize(0.85)
            group.get('b').normalize(0.90)
        group.reset()
        assert group.get('a').count == 0
        assert group.get('b').count == 0

    def test_custom_params_propagate(self):
        group = AnisotropyCorrectorGroup(names=['test'], window_size=64, min_samples=5)
        c = group.get('test')
        assert c.window_size == 64
