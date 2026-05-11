"""Pinned metric definitions for the suite framework.

Definitions are FROZEN at schema_version=1. Any change requires a
schema_version bump and a new MLflow experiment per §6 of the design.
Golden-value tests in test_metrics.py make drift visible.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memex_eval.suite.base import ScenarioOutcome


_MLFLOW_KEY_SAFE_RE = re.compile(r'[^A-Za-z0-9_./\- ]')


def _sanitize_mlflow_key(key: str) -> str:
    """Collapse any character outside MLflow's allowed set to ``_``.

    MLflow allows alphanumerics, underscore, dash, period, slash, space.
    Scenario ids already match ``^[a-z0-9_]+$`` so this is a no-op in
    practice; the helper exists as defense against future id-format
    relaxations and to make the contract explicit.
    """
    return _MLFLOW_KEY_SAFE_RE.sub('_', key)


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


def aggregate_per_scenario(
    outcomes: list[ScenarioOutcome],
    *,
    run_replicates: int = 1,
) -> dict[str, float]:
    """Bin ScenarioOutcomes by scenario_id and emit per-scenario pass-rate metrics.

    Returns keys of the form:

    - ``scenario.<id>.pass_rate`` — pass count / non-skipped non-errored count
    - ``scenario.<id>.pass_count``
    - ``scenario.<id>.total`` — actual non-skipped non-errored count

    Plus two suite-level rollups:

    - ``suite.pass_rate_all`` — every scenario (mutating + non-mutating)
    - ``suite.pass_rate_non_mutating`` — scenarios where the actual replicate
      count equals ``run_replicates``; scenarios with ``replicates_override``
      run fewer times and produce zero-variance signal so they're excluded
      from the headline rate. **At ``run_replicates=1`` (CLI default for
      smoke runs) this collapses to ``suite.pass_rate_all`` because every
      scenario trivially reaches N >= 1**; use ``--replicates 5`` or higher
      to see the two metrics diverge.

    Outcomes with ``status in ('skip', 'error')`` are excluded from the
    rate (they don't contribute to pass/fail). ``xfail`` counts as pass,
    ``xpass`` counts as fail (matches the framework's existing pass-rate
    convention).
    """
    by_scenario: dict[str, list[ScenarioOutcome]] = defaultdict(list)
    for o in outcomes:
        if o.status in ('skip', 'error'):
            continue
        by_scenario[o.scenario_id].append(o)

    result: dict[str, float] = {}
    suite_pass_all = 0
    suite_total_all = 0
    suite_pass_nonmut = 0
    suite_total_nonmut = 0

    for sid, group in by_scenario.items():
        safe = _sanitize_mlflow_key(sid)
        n = len(group)
        passes = sum(1 for o in group if o.status in ('pass', 'xfail'))
        result[f'scenario.{safe}.pass_rate'] = passes / n if n else 0.0
        result[f'scenario.{safe}.pass_count'] = float(passes)
        result[f'scenario.{safe}.total'] = float(n)
        suite_pass_all += passes
        suite_total_all += n
        if n >= run_replicates:
            suite_pass_nonmut += passes
            suite_total_nonmut += n

    if suite_total_all:
        result['suite.pass_rate_all'] = suite_pass_all / suite_total_all
    if suite_total_nonmut:
        result['suite.pass_rate_non_mutating'] = suite_pass_nonmut / suite_total_nonmut
    return result


__all__ = [
    'recall_at_k',
    'mrr',
    'ndcg_at_k',
    'percentile',
    'aggregate_metric_keys',
    'aggregate_per_scenario',
]
