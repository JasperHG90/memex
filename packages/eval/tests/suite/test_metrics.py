"""Golden-value tests for suite metrics. Pinned at schema_version=1.

Any change here requires a schema_version bump and a new MLflow
experiment per the design.
"""

from __future__ import annotations

import math

from memex_eval.suite.metrics import (
    aggregate_metric_keys,
    mrr,
    ndcg_at_k,
    percentile,
    recall_at_k,
)


class TestRecallAtK:
    def test_full_recall(self) -> None:
        assert recall_at_k(['a', 'b', 'c'], ['a', 'c'], 3) == 1.0

    def test_partial_recall(self) -> None:
        assert recall_at_k(['x', 'a', 'y'], ['a', 'b'], 3) == 0.5

    def test_no_recall(self) -> None:
        assert recall_at_k(['x', 'y', 'z'], ['a'], 3) == 0.0

    def test_empty_results(self) -> None:
        assert recall_at_k([], ['a'], 3) == 0.0

    def test_empty_gold_returns_one_vacuously(self) -> None:
        assert recall_at_k(['a', 'b'], [], 3) == 1.0

    def test_capped_denominator_when_gold_larger_than_k(self) -> None:
        # |gold| > k → denominator capped at k, can still hit 1.0
        assert recall_at_k(['a', 'b'], ['a', 'b', 'c', 'd'], 2) == 1.0

    def test_dedup_results(self) -> None:
        assert recall_at_k(['a', 'a', 'b'], ['a', 'b'], 3) == 1.0


class TestMRR:
    def test_first_position(self) -> None:
        assert mrr(['a', 'b'], ['a']) == 1.0

    def test_third_position(self) -> None:
        assert mrr(['x', 'y', 'a'], ['a']) == 1.0 / 3

    def test_no_match(self) -> None:
        assert mrr(['x', 'y', 'z'], ['a']) == 0.0

    def test_empty_gold(self) -> None:
        assert mrr(['a', 'b'], []) == 0.0


class TestNDCGAtK:
    def test_perfect_ranking(self) -> None:
        # Both gold IDs at top-2 — ratio of DCG/IDCG should be 1.0
        assert ndcg_at_k(['a', 'b'], ['a', 'b'], 3) == 1.0

    def test_inverted_ranking(self) -> None:
        # Gold appears at position 3 only; DCG = 1/log2(4) = 0.5
        # IDCG with 1 gold = 1/log2(2) = 1.0
        assert math.isclose(ndcg_at_k(['x', 'y', 'a'], ['a'], 3), 0.5, abs_tol=1e-9)

    def test_empty_gold(self) -> None:
        assert ndcg_at_k(['a', 'b'], [], 3) == 1.0


class TestPercentile:
    def test_p50_odd(self) -> None:
        assert percentile([10, 20, 30, 40, 50], 50) == 30

    def test_p95(self) -> None:
        # nearest-rank: rank = ceil(95/100 * 5) = 5
        assert percentile([1, 2, 3, 4, 5], 95) == 5

    def test_empty(self) -> None:
        assert percentile([], 50) == 0.0


class TestAggregate:
    def test_mean_std_min_max(self) -> None:
        result = aggregate_metric_keys([{'pass': 1.0}, {'pass': 0.0}, {'pass': 1.0}])
        assert math.isclose(result['metric.pass.mean'], 2 / 3, abs_tol=1e-9)
        assert result['metric.pass.min'] == 0.0
        assert result['metric.pass.max'] == 1.0
        assert result['metric.pass.std'] > 0

    def test_empty_input(self) -> None:
        assert aggregate_metric_keys([]) == {}
