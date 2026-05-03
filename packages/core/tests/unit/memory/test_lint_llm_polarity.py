"""Unit tests for F10b polarity wrapper (memex_core.memory.lint_llm.polarity)."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memex_core.memory.lint_llm.polarity import (
    DEFAULT_POLARITY_THRESHOLD,
    PolarityClassifier,
    PolarityRateLimiter,
    gate_passes,
)
from memex_core.memory.lint_llm.types import PolarityLabel, PolarityResult


class TestGatePasses:
    def test_cosine_above_threshold_clears(self):
        assert gate_passes(0.8, None, surprise_threshold=0.7) is True

    def test_cosine_above_threshold_clears_even_when_polarity_low(self):
        assert gate_passes(0.8, 0.01, surprise_threshold=0.7) is True

    def test_cosine_below_threshold_polarity_above_clears(self):
        assert gate_passes(0.4, 0.7, surprise_threshold=0.7, polarity_threshold=0.6) is True

    def test_both_below_thresholds_blocks(self):
        assert gate_passes(0.4, 0.3, surprise_threshold=0.7, polarity_threshold=0.6) is False

    def test_polarity_none_below_cosine_blocks(self):
        assert gate_passes(0.5, None, surprise_threshold=0.7) is False

    def test_default_polarity_threshold(self):
        assert gate_passes(0.4, 0.61, surprise_threshold=0.7) is True
        assert gate_passes(0.4, 0.59, surprise_threshold=0.7) is False
        assert DEFAULT_POLARITY_THRESHOLD == 0.6


class TestPolarityRateLimiter:
    @pytest.mark.asyncio
    async def test_unlimited_when_cap_is_none(self):
        rl = PolarityRateLimiter(max_per_vault_per_hour=None)
        vault = uuid4()
        for _ in range(100):
            assert await rl.admit(vault) is True

    @pytest.mark.asyncio
    async def test_caps_per_vault(self):
        rl = PolarityRateLimiter(max_per_vault_per_hour=3)
        vault = uuid4()
        assert await rl.admit(vault) is True
        assert await rl.admit(vault) is True
        assert await rl.admit(vault) is True
        assert await rl.admit(vault) is False

    @pytest.mark.asyncio
    async def test_per_vault_isolation(self):
        rl = PolarityRateLimiter(max_per_vault_per_hour=2)
        a, b = uuid4(), uuid4()
        assert await rl.admit(a) is True
        assert await rl.admit(a) is True
        assert await rl.admit(a) is False
        assert await rl.admit(b) is True
        assert await rl.admit(b) is True
        assert await rl.admit(b) is False

    @pytest.mark.asyncio
    async def test_used_tracks_count(self):
        rl = PolarityRateLimiter(max_per_vault_per_hour=5)
        vault = uuid4()
        assert await rl.used(vault) == 0
        await rl.admit(vault)
        await rl.admit(vault)
        assert await rl.used(vault) == 2


class TestPolarityClassifier:
    @pytest.mark.asyncio
    async def test_classify_pair_returns_argmax_label(self):
        model = AsyncMock()
        model.classify = AsyncMock(
            return_value={'contradiction': 0.7, 'entailment': 0.2, 'neutral': 0.1}
        )
        clf = PolarityClassifier(model)
        result = await clf.classify_pair('a', 'b', vault_id=uuid4())
        assert result is not None
        assert result.label == PolarityLabel.CONTRADICTION
        assert result.contradiction_prob == pytest.approx(0.7)
        assert result.entailment_prob == pytest.approx(0.2)
        assert result.neutral_prob == pytest.approx(0.1)

    @pytest.mark.asyncio
    async def test_classify_pair_argmax_neutral(self):
        model = AsyncMock()
        model.classify = AsyncMock(
            return_value={'contradiction': 0.1, 'entailment': 0.3, 'neutral': 0.6}
        )
        clf = PolarityClassifier(model)
        result = await clf.classify_pair('a', 'b', vault_id=uuid4())
        assert result is not None
        assert result.label == PolarityLabel.NEUTRAL

    @pytest.mark.asyncio
    async def test_rate_limit_returns_none_over_cap(self):
        model = AsyncMock()
        model.classify = AsyncMock(
            return_value={'contradiction': 0.9, 'entailment': 0.05, 'neutral': 0.05}
        )
        clf = PolarityClassifier(
            model,
            rate_limiter=PolarityRateLimiter(max_per_vault_per_hour=1),
        )
        vault = uuid4()
        first = await clf.classify_pair('a', 'b', vault_id=vault)
        second = await clf.classify_pair('a', 'b', vault_id=vault)
        assert first is not None
        assert second is None
        assert model.classify.await_count == 1

    @pytest.mark.asyncio
    async def test_invalid_polarity_threshold_rejected(self):
        with pytest.raises(ValueError):
            PolarityClassifier(AsyncMock(), polarity_threshold=1.1)

    def test_polarity_result_label_coerces_string(self):
        result = PolarityResult(
            label='contradiction',  # type: ignore[arg-type]
            contradiction_prob=0.6,
            entailment_prob=0.2,
            neutral_prob=0.2,
        )
        assert result.label == PolarityLabel.CONTRADICTION

    def test_polarity_result_probabilities_sum_within_tolerance(self):
        result = PolarityResult(
            label=PolarityLabel.NEUTRAL,
            contradiction_prob=0.333,
            entailment_prob=0.333,
            neutral_prob=0.334,
        )
        assert result.label is PolarityLabel.NEUTRAL

    def test_polarity_result_rejects_probabilities_summing_below_tolerance(self):
        with pytest.raises(ValueError, match='must sum to ~1.0'):
            PolarityResult(
                label=PolarityLabel.NEUTRAL,
                contradiction_prob=0.1,
                entailment_prob=0.1,
                neutral_prob=0.1,
            )

    def test_polarity_result_rejects_probabilities_summing_above_tolerance(self):
        with pytest.raises(ValueError, match='must sum to ~1.0'):
            PolarityResult(
                label=PolarityLabel.CONTRADICTION,
                contradiction_prob=0.6,
                entailment_prob=0.6,
                neutral_prob=0.6,
            )
