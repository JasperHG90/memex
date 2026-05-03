"""MW exploration floor — ε-greedy injection of low-MW units.

Prevents rich-get-richer dynamics by occasionally surfacing memories that
haven't had a chance to demonstrate value.  See Memory Worth §5.3 and
cognitive-memory-research-report.md F33.

The injection happens after MMR diversity filtering but before the final
limit is applied.  Injected units carry ``exploration: True`` in their
metadata so the caller can distinguish them and route outcome signals
appropriately.

F22 ``edge_exploration``
========================

The same epsilon-greedy scaffolding generalises from MW to confidence
variance: ``inject_edge_exploration`` surfaces high-variance units
(uncertain edges) for re-validation, mirroring F33's low-MW path. Same
pattern, different signal — eligibility is variance > threshold rather
than total outcome count < threshold.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

import structlog

from memex_core.memory.confidence import (
    MAX_VARIANCE,
    extract_confidence_and_count,
    mean_and_variance,
)
from memex_core.memory.sql_models import ContentStatus, MemoryUnit


def _coerce_metadata_to_dict(value: Any) -> dict[str, Any]:
    """Best-effort coercion of a unit's existing metadata into a plain dict.

    Hermes round-2 MED: the prior ``isinstance(..., dict)`` check would
    silently drop a non-dict Mapping (e.g. a Pydantic model surfaced by a
    future SQLModel adapter change). Now we accept any Mapping and convert
    it to a dict so existing keys survive the injection annotation.

    SQLModel currently hydrates JSONB columns as plain dicts, so the
    Mapping branch is defensive — but the silent-key-loss hazard goes away.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    return {}


# Hermes round-21 HIGH: cross-path injection guard. Both exploration
# injectors annotate ``unit.unit_metadata`` in-place; if a unit is fed
# into both selection pools (in violation of the documented call-site
# invariant), the second injector could surface a unit already
# annotated by the first. This set is the runtime defence: a unit
# carrying ANY of these keys is excluded from re-injection.
_INJECTION_ANNOTATION_KEYS: frozenset[str] = frozenset({'exploration', 'edge_exploration'})


def _already_injected(unit: MemoryUnit) -> bool:
    """Return True if ``unit`` already carries any injection annotation.

    Hermes round-21 HIGH: belt-and-suspenders runtime guard against
    cross-path mutation pipelining. The retrieval engine's call-site
    invariant (disjoint pools) is the primary defence; this helper
    backs it up so a future caller that breaks the invariant does NOT
    get a unit with both ``exploration=True`` and
    ``edge_exploration=True``.

    Hermes round-22 HIGH: uses ``key in metadata`` (presence check)
    rather than ``metadata.get(key)`` (truthy check). The intent is
    "has this annotation been set", not "is this value truthy" — so
    a future caller setting ``exploration=False`` (e.g. for a
    "tested-and-passed" status) would still be excluded from
    re-injection. The injectors themselves only ever write ``True``,
    but the presence check is correct-by-construction.
    """
    metadata = _coerce_metadata_to_dict(unit.unit_metadata)
    return any(key in metadata for key in _INJECTION_ANNOTATION_KEYS)


logger = structlog.get_logger('memex.core.memory.retrieval.exploration')

# Default exploration probability (ε).  5% means roughly 1 in 20 retrieval
# calls will inject an exploration unit.
DEFAULT_EPSILON = 0.05

# Maximum number of exploration units to inject per retrieval call.
DEFAULT_MAX_INJECTIONS = 2

# A unit is considered "low-MW" if its total outcome count is below this
# threshold.  Cold-start units (0/0) always qualify.
DEFAULT_LOW_MW_THRESHOLD = 5

# F22: a unit is considered "high-variance" if the closed-form Beta(1, 1)
# posterior variance is at least this fraction of MAX_VARIANCE = 1/12.
DEFAULT_HIGH_VARIANCE_FRACTION = 0.5


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

    # Eligible: not already in results, ACTIVE, not deprioritized, and low total outcome count.
    # Excludes stale (superseded by reflection) and deprioritized units to avoid surfacing
    # content the system has already de-emphasised. Hermes round-21 HIGH:
    # also exclude units already carrying an injection annotation
    # (``exploration`` or ``edge_exploration``) so a caller that breaks
    # the disjoint-pool invariant cannot produce a unit annotated by
    # both paths.
    eligible = [
        u
        for u in all_candidates
        if u.id not in result_ids
        and not u.is_deprioritized
        and u.status == ContentStatus.ACTIVE
        and (u.success_co_count + u.failure_co_count) < low_mw_threshold
        and not _already_injected(u)
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

    Mutation contract (Hermes round-13 LOW)
    ---------------------------------------
    Mirrors :func:`inject_edge_exploration`: each injected ``MemoryUnit``
    has its ``unit_metadata`` attribute REPLACED with a fresh dict
    containing the existing keys plus ``exploration=True``. The original
    metadata dict on the unit is not mutated, but the attribute swap IS
    observable to any caller that holds the same object.

    Cross-function hazard: do NOT pipeline this injector and
    :func:`inject_edge_exploration` on the same candidate list. Both
    annotate ``unit_metadata`` in place; chaining them on overlapping
    pools could let one path see the other's annotation. The retrieval
    engine calls each injector at most once per request, with disjoint
    selection pools (the second injector's ``all_candidates`` excludes
    units already in ``results``), so the in-place attribute swap is
    safe at the documented call site only.
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
        metadata = _coerce_metadata_to_dict(unit.unit_metadata)
        metadata = {**metadata, 'exploration': True}
        # HAZARD (Hermes round-20 LOW): in-place attribute swap on a
        # caller-owned ``MemoryUnit``. Caller-snapshot invariant: the
        # retrieval engine MUST pass list-copy snapshots (not aliased
        # references) so this annotation does not leak into a parallel
        # scoring stage that expects pristine metadata. Avoid
        # ``copy.deepcopy`` here — this is a hot per-request path.
        unit.unit_metadata = metadata

    logger.debug(
        'exploration_injection',
        count=len(exploration_units),
        unit_ids=[str(u.id) for u in exploration_units],
    )

    return results + exploration_units


def _unit_variance(unit: MemoryUnit) -> float:
    confidence, evidence_count = extract_confidence_and_count(unit)
    _, variance = mean_and_variance(confidence, evidence_count)
    return variance


def select_edge_exploration_candidates(
    results: list[MemoryUnit],
    all_candidates: list[MemoryUnit],
    *,
    epsilon: float = DEFAULT_EPSILON,
    max_injections: int = DEFAULT_MAX_INJECTIONS,
    high_variance_fraction: float = DEFAULT_HIGH_VARIANCE_FRACTION,
) -> list[MemoryUnit]:
    """F22: select high-variance candidates for re-validation injection.

    Mirror of :func:`select_exploration_candidates` but eligibility is keyed
    on variance (not outcome count). Units whose closed-form Beta(1, 1)
    posterior variance is at least ``high_variance_fraction × MAX_VARIANCE``
    qualify. With probability ``epsilon``, up to ``max_injections`` of these
    are randomly selected and returned.
    """
    if random.random() > epsilon:
        return []

    threshold = high_variance_fraction * MAX_VARIANCE
    result_ids = {u.id for u in results}
    # Hermes round-21 HIGH: exclude units already carrying an injection
    # annotation (``exploration`` or ``edge_exploration``) so a caller
    # that breaks the disjoint-pool invariant cannot produce a unit
    # annotated by both paths.
    eligible = [
        u
        for u in all_candidates
        if u.id not in result_ids
        and not u.is_deprioritized
        and u.status == ContentStatus.ACTIVE
        and _unit_variance(u) >= threshold
        and not _already_injected(u)
    ]
    if not eligible:
        return []

    n = min(max_injections, len(eligible))
    return random.sample(eligible, n)


def inject_edge_exploration(
    results: list[MemoryUnit],
    all_candidates: list[MemoryUnit],
    *,
    epsilon: float = DEFAULT_EPSILON,
    max_injections: int = DEFAULT_MAX_INJECTIONS,
    high_variance_fraction: float = DEFAULT_HIGH_VARIANCE_FRACTION,
) -> list[MemoryUnit]:
    """F22: epsilon-greedy injection of high-variance edges for re-validation.

    Pairs with :func:`inject_exploration_units` (the F33 low-MW path) — same
    scaffolding, different signal. Injected units carry
    ``edge_exploration=True`` in their ``unit_metadata`` so the caller can
    distinguish them from F33 injections.

    Wiring status (Hermes round-14 LOW): this function ships INERTLY in the
    F22 PR — defined and tested but NOT yet called from
    :mod:`memex_core.memory.retrieval.engine`. F22's ship-time guard
    ``RetrievalConfig.certainty_modulation_enabled = False`` keeps the
    feature column-only; wiring the edge-exploration injector requires a
    paired ``edge_exploration_*`` config flag (mirroring F33's
    ``exploration_epsilon`` / ``exploration_max_injections``) which lands
    in the activation PR after operators observe the
    ``CONFIDENCE_VARIANCE_OBSERVED`` distribution post-backfill. The
    function is exposed publicly so the activation PR is a pure call-site
    + config addition with no signature churn here.

    TODO(F22-activation, Hermes round-15 LOW): wire this injector into
    ``RetrievalEngine._search`` alongside the F33 ``inject_exploration_units``
    call site at ``engine.py:736-749``. The activation PR also adds the
    ``edge_exploration_epsilon`` / ``edge_exploration_max_injections`` /
    ``edge_exploration_high_variance_fraction`` config knobs on
    ``RetrievalConfig``, gated on the same operator decision as the
    ``certainty_modulation_enabled`` flip described in the BACKLOG
    F22 entry ("Ship-time guard" bullet).

    Mutation contract (Hermes round-1 MED)
    --------------------------------------
    Each injected ``MemoryUnit`` has its ``unit_metadata`` REPLACED with a
    fresh dict containing the existing keys plus ``edge_exploration=True``.
    The original metadata dict on the unit is not mutated, but the unit's
    attribute assignment IS observable to any caller that holds the same
    object. This mirrors :func:`inject_exploration_units`. Callers MUST NOT
    feed the same ``MemoryUnit`` instances through both this path and
    another scoring stage that expects pristine metadata — and MUST treat
    units returned by this function as carrying the injection annotation
    until the request is fully served. The retrieval engine uses these
    objects exactly once per request (no cross-request reuse), so the
    in-place attribute swap is safe in the documented call site.

    Cross-function hazard (Hermes round-13 LOW): do NOT pipeline this
    injector and :func:`inject_exploration_units` (F33) on the same
    candidate list. Both annotate ``unit_metadata`` in place; chaining
    them on overlapping pools could surface a unit injected by F33 as a
    candidate for F22 edge exploration. The retrieval engine calls each
    injector at most once per request, with disjoint selection pools
    (the second injector's ``all_candidates`` excludes units already in
    ``results``), so the in-place attribute swap is safe at the
    documented call site only.
    """
    edges = select_edge_exploration_candidates(
        results,
        all_candidates,
        epsilon=epsilon,
        max_injections=max_injections,
        high_variance_fraction=high_variance_fraction,
    )
    if not edges:
        return results

    for unit in edges:
        metadata = _coerce_metadata_to_dict(unit.unit_metadata)
        metadata = {**metadata, 'edge_exploration': True}
        # HAZARD (Hermes round-20 LOW): in-place attribute swap on a
        # caller-owned ``MemoryUnit``. Caller-snapshot invariant: the
        # F22-activation wiring (see TODO above) MUST pass list-copy
        # snapshots so this annotation does not leak into a parallel
        # scoring stage. Avoid ``copy.deepcopy`` here — this is a hot
        # per-request path.
        # TODO(F22-activation): when wiring this injector into
        # ``RetrievalEngine._search``, audit the call site for
        # snapshot-by-caller invariants alongside the F33
        # ``inject_exploration_units`` site.
        unit.unit_metadata = metadata

    logger.debug(
        'edge_exploration_injection',
        count=len(edges),
        unit_ids=[str(u.id) for u in edges],
    )

    return results + edges
