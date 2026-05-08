"""Pinned metric definitions for the suite framework.

Definitions are FROZEN at schema_version=1. Any change requires a
schema_version bump and a new MLflow experiment per §6 of the design.
Golden-value tests in test_metrics.py make drift visible.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _dedup_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def recall_at_k(retrieved: list[str], gold: list[str], k: int) -> float:
    """|gold ∩ top_k(dedup_results)| / min(|gold|, k).

    Capped denominator so suites where |gold| > k can still hit 1.0.
    Empty gold → 1.0 (vacuous).
    """
    gold_set = set(gold)
    if not gold_set:
        return 1.0
    top_k = _dedup_preserve_order(retrieved)[:k]
    hits = sum(1 for r in top_k if r in gold_set)
    denom = min(len(gold_set), k)
    return hits / denom if denom else 0.0


def mrr(retrieved: list[str], gold: list[str]) -> float:
    """1 / rank_of_first_gold(dedup_results), or 0.0 if none.

    Empty gold → 0.0 (no signal to measure).
    """
    gold_set = set(gold)
    if not gold_set:
        return 0.0
    for rank, r in enumerate(_dedup_preserve_order(retrieved), start=1):
        if r in gold_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: list[str], gold: list[str], k: int) -> float:
    """Binary-relevance NDCG@k.

    DCG@k = sum_{i=1..k} rel_i / log2(i+1) with rel_i ∈ {0, 1}.
    IDCG@k = sum_{i=1..min(|gold|, k)} 1 / log2(i+1).
    Empty gold → 1.0 (vacuous).
    """
    import math

    gold_set = set(gold)
    if not gold_set:
        return 1.0
    top_k = _dedup_preserve_order(retrieved)[:k]
    dcg = sum((1.0 / math.log2(i + 2)) for i, r in enumerate(top_k) if r in gold_set)
    ideal_n = min(len(gold_set), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_n))
    return dcg / idcg if idcg else 0.0


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile in [0, 100]. Returns 0.0 on empty.

    Uses the standard nearest-rank formula: rank = ceil(p/100 * N).
    """
    import math

    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    rank = max(1, math.ceil(p / 100.0 * len(sorted_vals)))
    return sorted_vals[rank - 1]


def aggregate_metric_keys(per_scenario: list[dict[str, float]]) -> dict[str, float]:
    """Collect per-scenario metric dicts → {metric.<key>.{mean,std,min,max}}."""
    import statistics

    by_key: dict[str, list[float]] = {}
    for d in per_scenario:
        for k, v in d.items():
            by_key.setdefault(k, []).append(v)

    out: dict[str, float] = {}
    for key, values in by_key.items():
        if not values:
            continue
        out[f'metric.{key}.mean'] = statistics.fmean(values)
        out[f'metric.{key}.std'] = statistics.pstdev(values) if len(values) > 1 else 0.0
        out[f'metric.{key}.min'] = min(values)
        out[f'metric.{key}.max'] = max(values)
    return out


__all__ = [
    'recall_at_k',
    'mrr',
    'ndcg_at_k',
    'percentile',
    'aggregate_metric_keys',
]
