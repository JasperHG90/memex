"""MW exploration floor — ε-greedy injection of low-MW units.

Prevents rich-get-richer dynamics by occasionally surfacing memories that
haven't had a chance to demonstrate value.  See Memory Worth §5.3 and
cognitive-memory-research-report.md F33.

The injection happens after MMR diversity filtering but before the final
limit is applied.  Injected units carry ``exploration: True`` in their
metadata so the caller can distinguish them and route outcome signals
appropriately.
"""

from __future__ import annotations

import random

import structlog

from memex_core.memory.sql_models import MemoryUnit

logger = structlog.get_logger('memex.core.memory.retrieval.exploration')

# Default exploration probability (ε).  5% means roughly 1 in 20 retrieval
# calls will inject an exploration unit.
DEFAULT_EPSILON = 0.05

# Maximum number of exploration units to inject per retrieval call.
DEFAULT_MAX_INJECTIONS = 2

# A unit is considered "low-MW" if its total outcome count is below this
# threshold.  Cold-start units (0/0) always qualify.
DEFAULT_LOW_MW_THRESHOLD = 5


def select_exploration_candidates(
    results: list[MemoryUnit],
    all_candidates: list[MemoryUnit],
    *,
    epsilon: float = DEFAULT_EPSILON,
    max_injections: int = DEFAULT_MAX_INJECTIONS,
    low_mw_threshold: int = DEFAULT_LOW_MW_THRESHOLD,
) -> list[MemoryUnit]:
    """Select exploration candidates from the pool of unselected units.

    Only units with low total outcome counts (success + failure < threshold)
    are eligible.  With probability ε, up to ``max_injections`` of these are
    randomly selected and returned.

    Args:
        results: Already-selected retrieval results (will NOT be modified).
        all_candidates: Full pool of hydrated candidates (including those
            already in ``results``).
        epsilon: Exploration probability per retrieval call.
        max_injections: Maximum number of exploration units to inject.
        low_mw_threshold: Units with (success_co_count + failure_co_count)
            below this threshold are eligible for exploration.

    Returns:
        List of exploration-injection units (may be empty).
    """
    if random.random() > epsilon:
        return []

    result_ids = {u.id for u in results}

    # Eligible: not already in results, and low total outcome count
    eligible = [
        u
        for u in all_candidates
        if u.id not in result_ids and (u.success_co_count + u.failure_co_count) < low_mw_threshold
    ]

    if not eligible:
        return []

    n = min(max_injections, len(eligible))
    return random.sample(eligible, n)


def inject_exploration_units(
    results: list[MemoryUnit],
    all_candidates: list[MemoryUnit],
    *,
    epsilon: float = DEFAULT_EPSILON,
    max_injections: int = DEFAULT_MAX_INJECTIONS,
    low_mw_threshold: int = DEFAULT_LOW_MW_THRESHOLD,
) -> list[MemoryUnit]:
    """Inject exploration units into retrieval results.

    Calls ``select_exploration_candidates`` and appends the selected units
    to the end of ``results``, marking each with ``exploration=True`` in
    ``unit.metadata`` (stored in the ``unit_metadata`` JSONB column).

    Args:
        results: Already-selected retrieval results (NOT modified in-place).
        all_candidates: Full pool of hydrated candidates.
        epsilon: Exploration probability per retrieval call.
        max_injections: Maximum number of exploration units to inject.
        low_mw_threshold: Low-MW eligibility threshold.

    Returns:
        New list with exploration units appended (if selected).
    """
    exploration_units = select_exploration_candidates(
        results,
        all_candidates,
        epsilon=epsilon,
        max_injections=max_injections,
        low_mw_threshold=low_mw_threshold,
    )

    if not exploration_units:
        return results

    for unit in exploration_units:
        metadata = unit.unit_metadata if isinstance(unit.unit_metadata, dict) else {}
        metadata = {**metadata, 'exploration': True}
        unit.unit_metadata = metadata

    logger.debug(
        'exploration_injection',
        count=len(exploration_units),
        unit_ids=[str(u.id) for u in exploration_units],
    )

    return results + exploration_units
