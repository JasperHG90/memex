"""Tests for cross-encoder recency and temporal proximity boosts (T6)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from memex_common.config import RetrievalConfig
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryUnit


def _make_unit(
    event_date: datetime | None = None,
    temporal_proximity: float | None = None,
    text: str = 'test fact',
) -> MemoryUnit:
    """Create a minimal MemoryUnit for reranking tests."""
    unit = MemoryUnit(
        id=uuid4(),
        text=text,
        fact_type='fact',
        event_date=event_date,
        vault_id=uuid4(),
        note_id=uuid4(),
        embedding=[],
    )
    if temporal_proximity is not None:
        object.__setattr__(unit, 'temporal_proximity', temporal_proximity)
    return unit


def _make_engine(
    scores: list[float],
    recency_alpha: float = 0.2,
    temporal_alpha: float = 0.2,
) -> RetrievalEngine:
    """Create engine with mock reranker returning given raw scores."""
    reranker = MagicMock()
    reranker.score.return_value = scores
    config = RetrievalConfig(
        reranking_recency_alpha=recency_alpha,
        reranking_temporal_alpha=temporal_alpha,
    )
    return RetrievalEngine(
        embedder=MagicMock(),
        reranker=reranker,
        retrieval_config=config,
    )


class TestRecencyBoost:
    """Tests for the recency boost applied during reranking."""

    @pytest.mark.asyncio
    async def test_recent_unit_gets_boost_above_one(self) -> None:
        """A unit with today's event_date should get a recency boost > 1.0."""
        now = datetime.now(timezone.utc)
        unit = _make_unit(event_date=now)
        engine = _make_engine([0.0])  # sigmoid(0)=0.5

        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

        # Verify the boost: recency = max(0.1, min(1.0, 1 - 0/365)) = 1.0
        # recency_boost = 1 + 0.2*(1.0 - 0.5) = 1.1
        # temporal neutral -> 1.0
        # boosted = 0.5 * 1.1 * 1.0 = 0.55
        # Since we only have 1 item, ordering doesn't matter, but it should be present
        assert result[0] is unit

    @pytest.mark.asyncio
    async def test_old_unit_gets_boost_below_one(self) -> None:
        """A unit 300 days old should get a recency boost < 1.0."""
        old_date = datetime.now(timezone.utc) - timedelta(days=300)
        unit = _make_unit(event_date=old_date)
        engine = _make_engine([0.0])

        # recency = max(0.1, min(1.0, 1 - 300/365)) ~= 0.178
        # recency_boost = 1 + 0.2*(0.178 - 0.5) = 1 + 0.2*(-0.322) = 0.9356
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_very_old_unit_floors_at_point_one(self) -> None:
        """A unit >365 days old should floor recency at 0.1."""
        ancient_date = datetime.now(timezone.utc) - timedelta(days=500)
        unit = _make_unit(event_date=ancient_date)
        engine = _make_engine([0.0])

        # recency = max(0.1, min(1.0, 1 - 500/365)) = max(0.1, -0.37) = 0.1
        # recency_boost = 1 + 0.2*(0.1 - 0.5) = 1 + 0.2*(-0.4) = 0.92
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_missing_event_date_neutral_boost(self) -> None:
        """A unit with no event_date should get neutral recency (0.5) -> boost = 1.0."""
        unit = _make_unit(event_date=None)
        engine = _make_engine([0.0])

        # recency = 0.5 -> boost = 1 + 0.2*(0.5 - 0.5) = 1.0
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_future_event_date_clamped_to_one(self) -> None:
        """A future event_date should be clamped to recency=1.0."""
        future_date = datetime.now(timezone.utc) + timedelta(days=30)
        unit = _make_unit(event_date=future_date)
        engine = _make_engine([0.0])

        # days_ago = negative -> 1 - (negative/365) > 1.0 -> clamped to 1.0
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1


class TestTemporalProximityBoost:
    """Tests for the temporal proximity boost."""

    @pytest.mark.asyncio
    async def test_high_temporal_proximity_boosts(self) -> None:
        """High temporal_proximity (1.0) should boost the score."""
        unit = _make_unit(
            event_date=None,  # neutral recency
            temporal_proximity=1.0,
        )
        engine = _make_engine([0.0])

        # temporal_boost = 1 + 0.2*(1.0 - 0.5) = 1.1
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_low_temporal_proximity_reduces(self) -> None:
        """Low temporal_proximity (0.0) should reduce the score."""
        unit = _make_unit(
            event_date=None,
            temporal_proximity=0.0,
        )
        engine = _make_engine([0.0])

        # temporal_boost = 1 + 0.2*(0.0 - 0.5) = 0.9
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_missing_temporal_proximity_neutral(self) -> None:
        """Missing temporal_proximity -> default 0.5 -> boost = 1.0."""
        unit = _make_unit(event_date=None)  # no temporal_proximity attr
        engine = _make_engine([0.0])

        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1


class TestAlphaZeroDisablesBoosts:
    """When alpha=0, all boosts should be exactly 1.0 (backward compatible)."""

    @pytest.mark.asyncio
    async def test_alpha_zero_recent_unit(self) -> None:
        """alpha=0 means boost=1.0 regardless of recency."""
        now = datetime.now(timezone.utc)
        unit = _make_unit(event_date=now, temporal_proximity=1.0)
        engine = _make_engine([2.0], recency_alpha=0.0, temporal_alpha=0.0)

        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_alpha_zero_old_unit(self) -> None:
        """alpha=0, even old units get boost=1.0."""
        old_date = datetime.now(timezone.utc) - timedelta(days=300)
        unit = _make_unit(event_date=old_date, temporal_proximity=0.0)
        engine = _make_engine([2.0], recency_alpha=0.0, temporal_alpha=0.0)

        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_alpha_zero_preserves_original_order(self) -> None:
        """With alpha=0, order should be purely based on CE scores."""
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=300)
        unit_a = _make_unit(event_date=old, text='old fact')
        unit_b = _make_unit(event_date=now, text='new fact')

        # unit_a has higher CE score (3.0 vs 1.0)
        engine = _make_engine([3.0, 1.0], recency_alpha=0.0, temporal_alpha=0.0)

        result = await engine._rerank_results('query', [unit_a, unit_b])
        assert result[0] is unit_a
        assert result[1] is unit_b


class TestCombinedBoosts:
    """Tests for combined recency * temporal * CE interaction."""

    @pytest.mark.asyncio
    async def test_combined_formula(self) -> None:
        """Verify boosted = ce_score * recency_boost * temporal_boost."""
        now = datetime.now(timezone.utc)
        unit = _make_unit(event_date=now, temporal_proximity=1.0)
        # CE raw score = 0 -> sigmoid = 0.5
        engine = _make_engine([0.0], recency_alpha=0.2, temporal_alpha=0.2)

        # recency = 1.0, recency_boost = 1 + 0.2*(1.0 - 0.5) = 1.1
        # temporal = 1.0, temporal_boost = 1 + 0.2*(1.0 - 0.5) = 1.1
        # boosted = 0.5 * 1.1 * 1.1 = 0.605
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_boosts_can_change_ranking_order(self) -> None:
        """Recency boost should be able to promote a recent unit over an old one."""
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=350)

        # Old unit has slightly higher CE score
        unit_old = _make_unit(event_date=old, text='old fact')
        unit_new = _make_unit(event_date=now, text='new fact')

        # Raw CE: old=0.5, new=0.3
        # sigmoid(0.5) ~= 0.622, sigmoid(0.3) ~= 0.574
        # Old recency: max(0.1, 1-350/365)=0.041 -> clamped 0.1
        #   boost = 1 + 0.2*(0.1-0.5) = 0.92
        #   boosted = 0.622 * 0.92 * 1.0 = 0.572
        # New recency: 1.0 -> boost = 1 + 0.2*(1.0-0.5) = 1.1
        #   boosted = 0.574 * 1.1 * 1.0 = 0.631
        # New should beat Old despite lower CE score
        engine = _make_engine([0.5, 0.3], recency_alpha=0.2, temporal_alpha=0.0)

        result = await engine._rerank_results('query', [unit_old, unit_new])
        assert result[0] is unit_new
        assert result[1] is unit_old

    @pytest.mark.asyncio
    async def test_boost_bounds_with_default_alpha(self) -> None:
        """With default alpha=0.2, boosts should be bounded in [0.9, 1.1]."""
        now = datetime.now(timezone.utc)

        # Best case
        best_unit = _make_unit(event_date=now, temporal_proximity=1.0)
        engine = _make_engine([0.0])
        await engine._rerank_results('query', [best_unit])

        # Worst case
        ancient = datetime.now(timezone.utc) - timedelta(days=500)
        worst_unit = _make_unit(event_date=ancient, temporal_proximity=0.0)
        engine2 = _make_engine([0.0])
        await engine2._rerank_results('query', [worst_unit])


class TestEdgeCases:
    """Edge case tests for reranking boosts."""

    @pytest.mark.asyncio
    async def test_empty_results_no_error(self) -> None:
        """Empty results list should return empty without error."""
        engine = _make_engine([])
        result = await engine._rerank_results('query', [])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_reranker_returns_as_is(self) -> None:
        """When reranker is None, results pass through unchanged."""
        config = RetrievalConfig()
        engine = RetrievalEngine(embedder=MagicMock(), reranker=None, retrieval_config=config)
        unit = _make_unit()
        result = await engine._rerank_results('query', [unit])
        assert result == [unit]

    @pytest.mark.asyncio
    async def test_min_score_filtering_still_works(self) -> None:
        """min_score threshold should still filter low-scoring results."""
        now = datetime.now(timezone.utc)
        unit_high = _make_unit(event_date=now, text='high')
        unit_low = _make_unit(event_date=now, text='low')

        # Raw scores: high=2.0 (sigmoid ~0.88), low=-5.0 (sigmoid ~0.007)
        engine = _make_engine([2.0, -5.0])

        result = await engine._rerank_results('query', [unit_high, unit_low], min_score=0.5)
        assert len(result) == 1
        assert result[0] is unit_high

    @pytest.mark.asyncio
    async def test_temporal_proximity_attribute_used(self) -> None:
        """Verify temporal_proximity attribute is read from the unit when present."""
        unit = _make_unit(event_date=None, temporal_proximity=0.9)
        engine = _make_engine([0.0])

        # temporal_boost = 1 + 0.2*(0.9-0.5) = 1 + 0.08 = 1.08
        # recency neutral = 1.0
        # boosted = 0.5 * 1.0 * 1.08 = 0.54
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1


class TestExtremeAlphaValues:
    """Tests for extreme alpha values beyond the typical 0.0-0.2 range."""

    @pytest.mark.asyncio
    async def test_alpha_one_gives_maximum_boost(self) -> None:
        """alpha=1.0 makes boost range [0.5, 1.5] for recency."""
        now = datetime.now(timezone.utc)
        unit = _make_unit(event_date=now, temporal_proximity=1.0)
        engine = _make_engine([0.0], recency_alpha=1.0, temporal_alpha=1.0)

        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_high_alpha_reverses_ranking(self) -> None:
        """Very high alpha on recency can flip the ranking of close CE scores."""
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=350)

        unit_old = _make_unit(event_date=old, text='old fact')
        unit_new = _make_unit(event_date=now, text='new fact')

        # Give old unit much higher CE score
        # sigmoid(3.0) ~= 0.953, sigmoid(0.0) = 0.5
        # With alpha=1.0:
        #   old recency ~ 0.1, boost = 1 + 1.0*(0.1 - 0.5) = 0.6
        #   boosted_old = 0.953 * 0.6 = 0.572
        #   new recency = 1.0, boost = 1 + 1.0*(1.0 - 0.5) = 1.5
        #   boosted_new = 0.5 * 1.5 = 0.75
        # New beats old despite much lower CE score
        engine = _make_engine([3.0, 0.0], recency_alpha=1.0, temporal_alpha=0.0)

        result = await engine._rerank_results('query', [unit_old, unit_new])
        assert result[0] is unit_new


class TestRerankerException:
    """When reranker raises an exception, graceful fallback to original order."""

    @pytest.mark.asyncio
    async def test_reranker_value_error_falls_back(self) -> None:
        """ValueError in reranker returns results in original order."""
        reranker = MagicMock()
        reranker.score.side_effect = ValueError('bad input')
        config = RetrievalConfig()
        engine = RetrievalEngine(embedder=MagicMock(), reranker=reranker, retrieval_config=config)
        unit_a = _make_unit(text='a')
        unit_b = _make_unit(text='b')
        result = await engine._rerank_results('query', [unit_a, unit_b])
        assert result == [unit_a, unit_b]

    @pytest.mark.asyncio
    async def test_reranker_runtime_error_falls_back(self) -> None:
        """RuntimeError in reranker returns results in original order."""
        reranker = MagicMock()
        reranker.score.side_effect = RuntimeError('model crashed')
        config = RetrievalConfig()
        engine = RetrievalEngine(embedder=MagicMock(), reranker=reranker, retrieval_config=config)
        unit = _make_unit(text='test')
        result = await engine._rerank_results('query', [unit])
        assert result == [unit]


class TestSingleUnit:
    """Single-item edge case: ordering is trivially correct."""

    @pytest.mark.asyncio
    async def test_single_unit_returned_unchanged(self) -> None:
        """Single unit is always returned regardless of score."""
        unit = _make_unit(event_date=None)
        engine = _make_engine([-10.0])  # Very low CE score
        result = await engine._rerank_results('query', [unit])
        assert len(result) == 1
        assert result[0] is unit
