"""FSFM-inspired graph-aware deprioritization scoring.

Computes a composite per-unit "should we deprioritize this?" score that the
linter and the auto-band step both consume. The composite blends four
signals, all computed at the unit level:

- ``graph_pressure``: signed aggregate over inbound ``MemoryLink`` rows.
  ``contradicts`` and ``weakens`` raise the score; ``reinforces`` and the
  causal types lower it. Each link is weighted by its own ``weight``,
  the source unit's credibility (``confidence × Memory Worth``), and a
  per-link recency decay. The signed sum is squashed through a sigmoid to
  ``[0, 1]`` (0.5 = no signal).
- ``memory_worth``: the existing Beta-Bernoulli posterior
  ``(success+1)/(success+failure+2)``. Enters the composite as
  ``1 - mw_score`` so low MW raises the score.
- ``temporal_staleness``: ``1 - exp(-age_days / stability)``, where
  ``stability`` is the per-class baseline already populated by
  migration 032 (``permanent → NULL`` ⇒ no decay component).
- ``entity_dormancy``: ``1 - exp(-mu × age_of_freshest_entity_mention)``,
  driven by ``entities.last_seen`` joined via ``unit_entities``.

Hard overrides return ``score == 0`` (and ``is_protected=True``) for:
``risk_class IN {sensitive, private, safety}``, ``intent_class=permanent``,
``status='stale'``, or ``is_deprioritized=true``.

The Python composite here is the canonical implementation; the linter's
``select_sql`` mirrors it as a SQL CTE. A SQL ↔ Python parity test guards
drift between the two paths (same pattern used for ``_MW_SCORE_EXPR``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.services.base import BaseService
from memex_core.services.outcomes import compute_mw_score

logger = structlog.get_logger(__name__)


# Per-link-type contribution sign.
#  +1.0 / +0.5 → deprioritize pressure (something is wrong with this unit)
#  -1.0 / -0.1 → keep pressure (this unit is supported / structurally embedded)
#   0.0        → neutral: don't enter the score
_LINK_TYPE_WEIGHT: dict[str, float] = {
    'contradicts': 1.0,
    'weakens': 0.5,
    'reinforces': -1.0,
    'causes': -0.1,
    'caused_by': -0.1,
    'enables': -0.1,
    'prevents': -0.1,
    # 'temporal' / 'semantic' / 'entity' are clustering hints, not quality
    # judgments; they do not contribute to deprioritization pressure.
}

PROTECTED_RISK_CLASSES = frozenset({'sensitive', 'private', 'safety'})

DEFAULT_WEIGHTS = {'graph': 0.5, 'mw': 0.25, 'temporal': 0.15, 'entity': 0.10}
DEFAULT_LAMBDA_LINK = 0.01
DEFAULT_MU_ENTITY = 0.005


def _sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _days_between(later: datetime, earlier: datetime | None) -> float:
    """Days between two timezone-aware datetimes; ``None`` earlier → 0.0.

    Negative deltas (clock skew, future-dated rows) clamp to 0.0 so a single
    bad timestamp doesn't drive a unit's score sideways.
    """
    if earlier is None:
        return 0.0
    seconds = (later - earlier).total_seconds()
    if seconds <= 0:
        return 0.0
    return seconds / 86400.0


@dataclass(frozen=True)
class _InboundLink:
    """Slice of one inbound ``MemoryLink`` row + its source unit's signals."""

    link_type: str
    link_weight: float
    link_created_at: datetime
    src_confidence: float
    src_success_co_count: int
    src_failure_co_count: int


@dataclass(frozen=True)
class _UnitInputs:
    """All per-unit inputs consumed by ``compute_composite``."""

    unit_id: UUID
    status: str
    is_deprioritized: bool
    risk_class: str | None
    intent_class: str | None
    importance: float | None
    stability: float | None
    success_co_count: int
    failure_co_count: int
    last_outcome_at: datetime | None
    inbound_links: tuple[_InboundLink, ...]
    freshest_entity_last_seen: datetime | None


# ---------------------------------------------------------------------------
# Pure-Python component math (canonical; SQL CTE in lint.py mirrors this)
# ---------------------------------------------------------------------------


def compute_graph_pressure(
    inbound_links: Iterable[_InboundLink],
    *,
    now: datetime,
    lambda_link: float = DEFAULT_LAMBDA_LINK,
) -> float:
    """Squashed signed aggregate of inbound link signals.

    Returns ``0.5`` when there are no signal-bearing inbound links, ``> 0.5``
    when contradiction/weakening pressure dominates, ``< 0.5`` when
    reinforcement / causal-anchor support dominates.
    """
    raw = 0.0
    for link in inbound_links:
        sign = _LINK_TYPE_WEIGHT.get(link.link_type, 0.0)
        if sign == 0.0:
            continue
        credibility = link.src_confidence * compute_mw_score(
            link.src_success_co_count, link.src_failure_co_count
        )
        recency = math.exp(-lambda_link * _days_between(now, link.link_created_at))
        raw += sign * link.link_weight * credibility * recency
    return _sigmoid(raw)


def compute_memory_worth_complement(success: int, failure: int) -> float:
    """``1 - mw_score``: low MW raises the deprioritize score."""
    return 1.0 - compute_mw_score(success, failure)


def compute_temporal_staleness(
    last_outcome_at: datetime | None,
    stability: float | None,
    *,
    now: datetime,
) -> float:
    """``1 - exp(-age_days / stability)``.

    NULL ``stability`` (permanent class baseline) or NULL ``last_outcome_at``
    return 0.0 — no temporal anchor, no decay pressure.
    """
    if last_outcome_at is None or stability is None or stability <= 0.0:
        return 0.0
    age_days = _days_between(now, last_outcome_at)
    return 1.0 - math.exp(-age_days / stability)


def compute_entity_dormancy(
    freshest_entity_last_seen: datetime | None,
    *,
    now: datetime,
    mu_entity: float = DEFAULT_MU_ENTITY,
) -> float:
    """``1 - exp(-mu × age_days)`` over the freshest linked entity's
    ``last_seen``. ``None`` (no linked entities or all NULL) → 0.0."""
    if freshest_entity_last_seen is None:
        return 0.0
    return 1.0 - math.exp(-mu_entity * _days_between(now, freshest_entity_last_seen))


def compute_composite(
    inputs: _UnitInputs,
    *,
    now: datetime,
    weights: dict[str, float] | None = None,
    lambda_link: float = DEFAULT_LAMBDA_LINK,
    mu_entity: float = DEFAULT_MU_ENTITY,
) -> tuple[float, dict[str, float], bool, str | None]:
    """Compute the composite score from already-fetched per-unit inputs.

    Returns ``(final_score, components, is_protected, protected_reason)``.
    The score is always in ``[0, 1]``; protected units short-circuit to 0.
    """
    w = weights or DEFAULT_WEIGHTS

    # Hard overrides — short-circuit before any expensive math.
    if inputs.is_deprioritized:
        return 0.0, {}, True, 'already_deprioritized'
    if inputs.status == 'stale':
        return 0.0, {}, True, 'status_stale'
    if inputs.risk_class in PROTECTED_RISK_CLASSES:
        return 0.0, {}, True, f'risk_class:{inputs.risk_class}'
    if inputs.intent_class == 'permanent':
        return 0.0, {}, True, 'intent_class:permanent'

    graph = compute_graph_pressure(inputs.inbound_links, now=now, lambda_link=lambda_link)
    mw_complement = compute_memory_worth_complement(
        inputs.success_co_count, inputs.failure_co_count
    )
    temporal = compute_temporal_staleness(inputs.last_outcome_at, inputs.stability, now=now)
    entity = compute_entity_dormancy(inputs.freshest_entity_last_seen, now=now, mu_entity=mu_entity)

    raw = (
        w['graph'] * graph
        + w['mw'] * mw_complement
        + w['temporal'] * temporal
        + w['entity'] * entity
    )

    # Importance is the FSFM-decay class baseline already cached by
    # migration 032 (permanent=1.0, durable=0.7, ephemeral=0.3, NULL=missing).
    # Multiply by (1 - importance) so high-importance units get suppressed
    # naturally. NULL importance is treated as 0.5 — neutral, no penalty.
    importance = inputs.importance if inputs.importance is not None else 0.5
    importance = max(0.0, min(1.0, importance))
    final = max(0.0, min(1.0, raw * (1.0 - importance)))

    components = {
        'graph_pressure': graph,
        'memory_worth_complement': mw_complement,
        'temporal_staleness': temporal,
        'entity_dormancy': entity,
    }
    return final, components, False, None


# ---------------------------------------------------------------------------
# Wire DTO + service
# ---------------------------------------------------------------------------


class ScoreBreakdown(BaseModel):
    """Per-unit deprioritization score breakdown returned from the scorer."""

    unit_id: UUID
    score: float = Field(..., ge=0.0, le=1.0)
    components: dict[str, float] = Field(default_factory=dict)
    importance: float | None = None
    is_protected: bool = False
    protected_reason: str | None = None
    explanation: dict[str, Any] = Field(default_factory=dict)


_FETCH_UNIT_INPUTS_SQL = text("""
    SELECT
        mu.id AS unit_id,
        mu.status AS status,
        mu.is_deprioritized AS is_deprioritized,
        mu.risk_class AS risk_class,
        mu.intent_class AS intent_class,
        mu.importance AS importance,
        mu.stability AS stability,
        mu.success_co_count AS success_co_count,
        mu.failure_co_count AS failure_co_count,
        mu.last_outcome_at AS last_outcome_at,
        (
            SELECT MAX(e.last_seen)
            FROM unit_entities ue
            JOIN entities e ON e.id = ue.entity_id
            WHERE ue.unit_id = mu.id
              AND ue.vault_id = :vault_id
        ) AS freshest_entity_last_seen
    FROM memory_units mu
    WHERE mu.id = :unit_id
      AND mu.vault_id = :vault_id
""")


_FETCH_INBOUND_LINKS_SQL = text("""
    SELECT
        ml.link_type AS link_type,
        ml.weight AS link_weight,
        ml.created_at AS link_created_at,
        src.confidence AS src_confidence,
        src.success_co_count AS src_success_co_count,
        src.failure_co_count AS src_failure_co_count
    FROM memory_links ml
    JOIN memory_units src ON src.id = ml.from_unit_id
    WHERE ml.to_unit_id = :unit_id
      AND ml.vault_id = :vault_id
      AND src.vault_id = :vault_id
""")


class DeprioritizeScorer(BaseService):
    """Computes the composite deprioritization score for memory units.

    The Python ``score()`` path is the canonical implementation used by:

    1. ``memex memory score <unit_id>`` (operator tuning surface).
    2. The auto-deprioritize step inside ``periodic_lint_task``.
    3. The SQL/Python parity test that guards drift between this code and
       the lint rules' inline CTEs.
    """

    def _config(self) -> dict[str, Any]:
        cfg = self.config.server.memory.deprioritize_score
        return {
            'weights': cfg.weights.as_dict(),
            'lambda_link': cfg.lambda_link,
            'mu_entity': cfg.mu_entity,
        }

    async def _load_unit_inputs(
        self,
        unit_id: UUID,
        vault_id: UUID,
        session: AsyncSession,
    ) -> _UnitInputs | None:
        row = (
            await session.execute(
                _FETCH_UNIT_INPUTS_SQL,
                {'unit_id': str(unit_id), 'vault_id': str(vault_id)},
            )
        ).one_or_none()
        if row is None:
            return None
        link_rows = (
            await session.execute(
                _FETCH_INBOUND_LINKS_SQL,
                {'unit_id': str(unit_id), 'vault_id': str(vault_id)},
            )
        ).all()
        inbound = tuple(
            _InboundLink(
                link_type=lr.link_type,
                link_weight=float(lr.link_weight),
                link_created_at=_ensure_aware(lr.link_created_at),
                src_confidence=float(lr.src_confidence),
                src_success_co_count=int(lr.src_success_co_count),
                src_failure_co_count=int(lr.src_failure_co_count),
            )
            for lr in link_rows
        )
        return _UnitInputs(
            unit_id=row.unit_id,
            status=row.status,
            is_deprioritized=bool(row.is_deprioritized),
            risk_class=row.risk_class,
            intent_class=row.intent_class,
            importance=(float(row.importance) if row.importance is not None else None),
            stability=(float(row.stability) if row.stability is not None else None),
            success_co_count=int(row.success_co_count),
            failure_co_count=int(row.failure_co_count),
            last_outcome_at=_ensure_aware_or_none(row.last_outcome_at),
            inbound_links=inbound,
            freshest_entity_last_seen=_ensure_aware_or_none(row.freshest_entity_last_seen),
        )

    async def score(
        self,
        unit_id: UUID,
        vault_id: UUID,
        session: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> ScoreBreakdown | None:
        """Score a single unit. Returns ``None`` if the unit doesn't exist
        in the vault (so the linter and CLI can distinguish from score=0)."""
        inputs = await self._load_unit_inputs(unit_id, vault_id, session)
        if inputs is None:
            return None
        cfg = self._config()
        resolved_now = now or datetime.now(timezone.utc)
        final, components, is_protected, protected_reason = compute_composite(
            inputs,
            now=resolved_now,
            weights=cfg['weights'],
            lambda_link=cfg['lambda_link'],
            mu_entity=cfg['mu_entity'],
        )
        return ScoreBreakdown(
            unit_id=unit_id,
            score=final,
            components=components,
            importance=inputs.importance,
            is_protected=is_protected,
            protected_reason=protected_reason,
            explanation={
                'inbound_link_count': len(inputs.inbound_links),
                'success_co_count': inputs.success_co_count,
                'failure_co_count': inputs.failure_co_count,
                'last_outcome_at': (
                    inputs.last_outcome_at.isoformat() if inputs.last_outcome_at else None
                ),
                'stability': inputs.stability,
                'intent_class': inputs.intent_class,
                'risk_class': inputs.risk_class,
                'freshest_entity_last_seen': (
                    inputs.freshest_entity_last_seen.isoformat()
                    if inputs.freshest_entity_last_seen
                    else None
                ),
                'protected_reason': protected_reason,
            },
        )


def _ensure_aware(dt: datetime) -> datetime:
    """Force a timezone-aware datetime (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _ensure_aware_or_none(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return _ensure_aware(dt)
