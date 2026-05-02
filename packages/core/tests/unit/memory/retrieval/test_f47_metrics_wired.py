"""F47: assert ``CONFIDENCE_BOOST_OBSERVED`` and ``CONFIDENCE_SCORE_DISTRIBUTION``
are registered Prometheus histograms with the documented bucket layouts."""

from __future__ import annotations

import pytest


class TestF47MetricsRegistered:
    def test_confidence_boost_observed_importable(self) -> None:
        from memex_core.metrics import CONFIDENCE_BOOST_OBSERVED

        assert CONFIDENCE_BOOST_OBSERVED is not None
        # Histogram instance exposes _name (prometheus_client internal).
        assert getattr(CONFIDENCE_BOOST_OBSERVED, '_name', '') == 'memex_confidence_boost'

    def test_confidence_score_distribution_importable(self) -> None:
        from memex_core.metrics import CONFIDENCE_SCORE_DISTRIBUTION

        assert CONFIDENCE_SCORE_DISTRIBUTION is not None
        assert getattr(CONFIDENCE_SCORE_DISTRIBUTION, '_name', '') == 'memex_confidence_score'

    def test_confidence_boost_buckets_match_mw_pattern(self) -> None:
        """CONFIDENCE_BOOST_OBSERVED mirrors MW_BOOST_OBSERVED's bucket layout
        (centred on neutral 1.0)."""
        from memex_core.metrics import CONFIDENCE_BOOST_OBSERVED, MW_BOOST_OBSERVED

        cb_buckets = getattr(CONFIDENCE_BOOST_OBSERVED, '_upper_bounds', ())
        mw_buckets = getattr(MW_BOOST_OBSERVED, '_upper_bounds', ())
        assert cb_buckets == mw_buckets, (
            'F47 boost histogram buckets must mirror the MW precedent so dashboards '
            f'can be templated. Got CB={cb_buckets} MW={mw_buckets}.'
        )

    def test_confidence_score_buckets_uniform_zero_to_one(self) -> None:
        """CONFIDENCE_SCORE_DISTRIBUTION uses uniform 0.0-1.0 buckets, mirroring
        MW_SCORE_DISTRIBUTION."""
        from memex_core.metrics import (
            CONFIDENCE_SCORE_DISTRIBUTION,
            MW_SCORE_DISTRIBUTION,
        )

        cs_buckets = getattr(CONFIDENCE_SCORE_DISTRIBUTION, '_upper_bounds', ())
        mw_buckets = getattr(MW_SCORE_DISTRIBUTION, '_upper_bounds', ())
        assert cs_buckets == mw_buckets, (
            'CONFIDENCE_SCORE_DISTRIBUTION must mirror MW_SCORE_DISTRIBUTION '
            f'bucket layout. Got CS={cs_buckets} MW={mw_buckets}.'
        )


class TestF47MetricsEmitDuringRerank:
    """``CONFIDENCE_BOOST_OBSERVED`` receives one observation per scored unit."""

    @pytest.mark.asyncio
    async def test_observed_metric_increments_during_rerank(self) -> None:
        from datetime import datetime, timezone
        from unittest.mock import MagicMock
        from uuid import uuid4

        from memex_common.config import RetrievalConfig
        from memex_core.memory.retrieval.engine import RetrievalEngine
        from memex_core.memory.sql_models import MemoryUnit
        from memex_core.metrics import CONFIDENCE_BOOST_OBSERVED

        unit = MemoryUnit(
            id=uuid4(),
            text='fact',
            fact_type='fact',
            event_date=datetime.now(timezone.utc),
            vault_id=uuid4(),
            note_id=uuid4(),
            embedding=[],
            success_co_count=0,
            failure_co_count=0,
            confidence=0.7,
        )
        reranker = MagicMock()
        reranker.score.return_value = [0.0]
        engine = RetrievalEngine(
            embedder=MagicMock(),
            reranker=reranker,
            retrieval_config=RetrievalConfig(confidence_alpha=0.3),
        )

        before = CONFIDENCE_BOOST_OBSERVED._sum.get()
        await engine._rerank_results('q', [unit])
        after = CONFIDENCE_BOOST_OBSERVED._sum.get()
        assert after > before, (
            'CONFIDENCE_BOOST_OBSERVED must record an observation per scored unit; '
            f'before={before} after={after}'
        )
