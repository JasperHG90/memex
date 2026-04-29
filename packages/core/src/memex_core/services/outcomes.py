"""Outcome recording service for Memory Worth (MW) counters.

Exposes `record_outcome()` for incrementing success/failure co-occurrence
counters on MemoryUnit, UnitEntity, and MentalModel, and `compute_mw_score() /
compute_mw_boost()` for the Beta-Bernoulli posterior mean used by the F1c
retrieval composition.

The MW formula uses additive-marginal composition (v6.9, §3.4):
    mw_score = (success_co_count + 1) / (success_co_count + failure_co_count + 2)
    mw_boost = 1.0 + mw_alpha * (mw_score - 0.5)

Cold-start units (0/0) get mw_score = 0.5 → mw_boost = 1.0 (neutral).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.metrics import OUTCOME_RECORDED_TOTAL, MW_SCORE_DISTRIBUTION

logger = structlog.get_logger(__name__)

MW_ALPHA_DEFAULT = 0.3
MW_PRIOR_ALPHA = 1  # Beta-Bernoulli prior α
MW_PRIOR_BETA = 1  # Beta-Bernoulli prior β


def compute_mw_score(success_co_count: int, failure_co_count: int) -> float:
    """Beta-Bernoulli posterior mean with α=β=1 uniform prior.

    Returns 0.5 for cold-start (0/0) — neutral, no penalty, no boost.
    """
    return (success_co_count + MW_PRIOR_ALPHA) / (
        success_co_count + failure_co_count + MW_PRIOR_ALPHA + MW_PRIOR_BETA
    )


def compute_mw_boost(
    success_co_count: int,
    failure_co_count: int,
    mw_alpha: float = MW_ALPHA_DEFAULT,
) -> float:
    """Additive-marginal MW boost factor for retrieval composition.

    Returns 1.0 for cold-start units (neutral — no rank change).
    """
    mw_score = compute_mw_score(success_co_count, failure_co_count)
    return 1.0 + mw_alpha * (mw_score - 0.5)


class OutcomeService:
    """Records outcome signals for Memory Worth counters."""

    def __init__(self) -> None:
        # No metastore/filestore dependency — callers pass the session.
        pass

    async def record_outcome(
        self,
        session: AsyncSession,
        unit_ids: list[str],
        success: bool,
        vault_id: str,
        outcome_confidence: float = 1.0,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record an outcome for one or more memory units.

        Increments success_co_count (success=True) or failure_co_count
        (success=False) on each unit, and propagates the counter increment
        to linked UnitEntity and MentalModel rows.

        Args:
            session: Active async DB session.
            unit_ids: UUIDs of the memory units that were load-bearing.
            success: True if the units contributed to a successful outcome.
            vault_id: Vault scope for the outcome.
            outcome_confidence: Weight for this outcome signal (0.0–1.0).
                Currently recorded but not used in counter arithmetic (v1
                uses integer increments; fractional weighting is F36).
            reason: Optional free-text reason (logged, not stored on units).

        Returns:
            Dict with counts of updated units, entities, and models.
        """
        from memex_core.memory.sql_models import MemoryUnit as MU

        counter_field = 'success_co_count' if success else 'failure_co_count'
        log = logger.bind(
            unit_count=len(unit_ids),
            outcome='success' if success else 'failure',
            vault_id=str(vault_id),
            confidence=outcome_confidence,
        )
        log.info('outcome.record', reason=reason)

        # Resolve valid unit IDs
        parsed_ids: list[UUID] = []
        for uid_str in unit_ids:
            try:
                parsed_ids.append(UUID(uid_str))
            except ValueError:
                log.warning('outcome.invalid_unit_id', unit_id=uid_str)
                continue

        if not parsed_ids:
            log.warning('outcome.no_valid_ids')
            return {'units_updated': 0, 'entities_updated': 0, 'models_updated': 0}

        # Increment counters on MemoryUnit rows
        result = await session.exec(
            select(MU).where(MU.id.in_(parsed_ids), MU.vault_id == UUID(vault_id))
        )
        units = result.all()

        for unit in units:
            current = getattr(unit, counter_field)
            setattr(unit, counter_field, current + 1)

            # Record MW score histogram observation
            mw_score = compute_mw_score(unit.success_co_count, unit.failure_co_count)
            MW_SCORE_DISTRIBUTION.labels(vault_id=str(vault_id)).observe(mw_score)

        await session.commit()

        # Propagate to UnitEntity rows
        entity_count = 0
        for unit in units:
            for ue in unit.unit_entities:
                current = getattr(ue, counter_field)
                setattr(ue, counter_field, current + 1)
                entity_count += 1

        # Propagate to MentalModel rows
        from memex_core.memory.sql_models import MentalModel as MM

        model_count = 0
        if units:
            all_entity_ids = list({ue.entity_id for unit in units for ue in unit.unit_entities})
            if all_entity_ids:
                models_result = await session.exec(
                    select(MM).where(
                        MM.entity_id.in_(all_entity_ids),
                        MM.vault_id == UUID(vault_id),
                    )
                )
                for model in models_result.all():
                    current = getattr(model, counter_field)
                    setattr(model, counter_field, current + 1)
                    model_count += 1

        await session.commit()

        OUTCOME_RECORDED_TOTAL.labels(
            vault_id=str(vault_id), outcome='success' if success else 'failure'
        ).inc(len(units))

        log.info(
            'outcome.recorded',
            units_updated=len(units),
            entities_updated=entity_count,
            models_updated=model_count,
        )

        return {
            'units_updated': len(units),
            'entities_updated': entity_count,
            'models_updated': model_count,
        }
