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
from sqlmodel import select, update
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
        unit_ids: list[str] | None,
        success: bool,
        vault_id: str | None = None,
        outcome_confidence: float = 1.0,
        reason: str | None = None,
        *,
        target_type: str = 'memory_unit',
        kv_key: str | None = None,
    ) -> dict[str, Any]:
        """Record an outcome.

        Two target modes (selected by keyword-only ``target_type``):

        * ``target_type='memory_unit'`` (default — existing positional path):
          increments success/failure co-counters on each MemoryUnit and
          propagates to linked UnitEntity + MentalModel rows. Requires
          positional ``unit_ids``.
        * ``target_type='kv_key'`` (F14 — added 2026-04-30): increments
          success/failure co-counters on the vault-scoped
          ``procedure_outcomes`` row matching ``(vault_id, kv_key)``. Row
          is upserted; ``last_outcome_at`` is set to ``now()``. Requires
          keyword-only ``kv_key`` (must be a ``procedure:<verb>:<tag>``
          key per RFC-007 §53-61) plus ``success`` and ``vault_id``.
          ``unit_ids`` MUST be ``None`` or empty for kv_key mode —
          mixing modes is an error (silent counter divergence otherwise).

        Both ``unit_ids`` and ``success`` are required (no defaults) so a
        caller cannot accidentally invoke ``record_outcome(session)`` and
        silently record a FAILURE outcome — the exact MW-signal-quality
        pathology F1 is built to detect.

        Args:
            session: Active async DB session.
            unit_ids: UUIDs of memory units (memory_unit mode only). Pass
                ``None`` or ``[]`` for kv_key mode.
            success: True if outcome was successful. Required — no default.
            vault_id: Vault scope for the outcome.
            outcome_confidence: Weight for this outcome signal (0.0–1.0).
                Currently recorded but not used in counter arithmetic (v1
                uses integer increments; fractional weighting is F36).
                # TODO(F36): fractional counter weighting
            reason: Optional free-text reason (logged, not stored on units).
            target_type: 'memory_unit' (default) or 'kv_key' (F14 procedure
                outcomes).
            kv_key: Procedure KV key (kv_key mode only).

        Returns:
            Dict with counts of updated rows. For ``memory_unit``:
            ``{units_updated, entities_updated, models_updated}``. For
            ``kv_key``: ``{kv_key, vault_id, success_co_count,
            failure_co_count, last_outcome_at}``.
        """
        if target_type == 'kv_key':
            if unit_ids:
                raise ValueError(
                    'kv_key mode does not accept unit_ids; pass unit_ids only '
                    'with target_type="memory_unit"'
                )
            return await self._record_outcome_kv_key(
                session=session,
                kv_key=kv_key,
                success=success,
                vault_id=vault_id,
                reason=reason,
            )
        if target_type != 'memory_unit':
            raise ValueError(f"target_type must be 'memory_unit' or 'kv_key', got {target_type!r}")
        if unit_ids is None or vault_id is None:
            raise ValueError("memory_unit mode requires 'unit_ids' and 'vault_id'.")

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

        # Validate vault_id
        try:
            vault_uuid = UUID(vault_id)
        except ValueError:
            raise ValueError(f'Invalid vault_id: {vault_id}')

        if not parsed_ids:
            log.warning('outcome.no_valid_ids')
            return {'units_updated': 0, 'entities_updated': 0, 'models_updated': 0}

        # Atomic increment on MemoryUnit rows (SQL-level, no race condition)
        stmt = (
            update(MU)
            .where(MU.id.in_(parsed_ids), MU.vault_id == vault_uuid)
            .values({counter_field: MU.__table__.c[counter_field] + 1})
        )
        result = await session.exec(stmt)
        units_updated = result.rowcount  # type: ignore[union-attr]

        # Propagate to UnitEntity rows (atomic increment)
        from memex_core.memory.sql_models import UnitEntity as UE

        entity_stmt = (
            update(UE)
            .where(
                UE.unit_id.in_(parsed_ids),
                UE.vault_id == vault_uuid,
            )
            .values({counter_field: UE.__table__.c[counter_field] + 1})
        )
        entity_result = await session.exec(entity_stmt)
        entity_count = entity_result.rowcount  # type: ignore[union-attr]

        # Propagate to MentalModel rows (atomic increment)
        from memex_core.memory.sql_models import MentalModel as MM

        model_stmt = (
            update(MM)
            .where(
                MM.entity_id.in_(
                    select(UE.entity_id).where(
                        UE.unit_id.in_(parsed_ids), UE.vault_id == vault_uuid
                    )
                ),
                MM.vault_id == vault_uuid,
            )
            .values({counter_field: MM.__table__.c[counter_field] + 1})
        )
        model_result = await session.exec(model_stmt)
        model_count = model_result.rowcount  # type: ignore[union-attr]

        # F38 diff hook: emit one audit row per unit so consolidation_tick can
        # find units with new outcome signal via AuditLog.action='outcome.record'.
        from memex_core.memory.sql_models import AuditLog

        outcome_label = 'success' if success else 'failure'
        for uid in parsed_ids:
            session.add(
                AuditLog(
                    action='outcome.record',
                    resource_type='memory_unit',
                    resource_id=str(uid),
                    details={'vault_id': str(vault_uuid), 'outcome': outcome_label},
                )
            )

        # Single commit for all three counter updates — preserves co-occurrence
        # invariant across MemoryUnit, UnitEntity, and MentalModel tables.
        await session.commit()

        # Observe MW scores post-commit (read-only, no commit needed)
        refreshed = await session.exec(
            select(MU).where(MU.id.in_(parsed_ids), MU.vault_id == vault_uuid)
        )
        for unit in refreshed.all():
            mw_score = compute_mw_score(unit.success_co_count, unit.failure_co_count)
            MW_SCORE_DISTRIBUTION.labels(vault_id=str(vault_id)).observe(mw_score)

        OUTCOME_RECORDED_TOTAL.labels(
            vault_id=str(vault_id), outcome='success' if success else 'failure'
        ).inc(units_updated)

        log.info(
            'outcome.recorded',
            units_updated=units_updated,
            entities_updated=entity_count,
            models_updated=model_count,
        )

        return {
            'units_updated': units_updated,
            'entities_updated': entity_count,
            'models_updated': model_count,
        }

    async def _record_outcome_kv_key(
        self,
        session: AsyncSession,
        kv_key: str | None,
        success: bool,
        vault_id: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        """F14: vault-scoped MW counter increment for a procedure KV key.

        Upserts into ``procedure_outcomes`` keyed on ``(vault_id, kv_key)``,
        atomically incrementing ``success_co_count`` (success=True) or
        ``failure_co_count`` (success=False) and stamping
        ``last_outcome_at = now()``.

        Validates that ``kv_key`` matches the
        ``procedure:<verb>:<context-tag>`` shape (RFC-007 §53-61) before
        touching the DB.
        """
        from memex_core.services.kv import validate_procedure_key

        if kv_key is None or vault_id is None:
            raise ValueError("kv_key mode requires 'kv_key' and 'vault_id'.")
        validate_procedure_key(kv_key)
        try:
            vault_uuid = UUID(vault_id)
        except ValueError as exc:
            raise ValueError(f'Invalid vault_id: {vault_id}') from exc

        log = logger.bind(
            kv_key=kv_key,
            vault_id=str(vault_id),
            outcome='success' if success else 'failure',
        )
        log.info('outcome.kv_key.record', reason=reason)

        from sqlalchemy import text as sql_text

        success_inc = 1 if success else 0
        failure_inc = 0 if success else 1
        # Upsert: INSERT ... ON CONFLICT (vault_id, kv_key) DO UPDATE.
        # Atomic at the row level — no read-modify-write window.
        upsert = sql_text(
            'INSERT INTO procedure_outcomes '
            '(vault_id, kv_key, success_co_count, failure_co_count, last_outcome_at) '
            'VALUES (:vid, :k, :sinc, :finc, now()) '
            'ON CONFLICT ON CONSTRAINT uq_procedure_outcomes_vault_key DO UPDATE SET '
            '  success_co_count = procedure_outcomes.success_co_count + EXCLUDED.success_co_count, '
            '  failure_co_count = procedure_outcomes.failure_co_count + EXCLUDED.failure_co_count, '
            '  last_outcome_at = EXCLUDED.last_outcome_at, '
            '  updated_at = now() '
            'RETURNING success_co_count, failure_co_count, last_outcome_at'
        )
        result = await session.execute(
            upsert,
            {
                'vid': vault_uuid,
                'k': kv_key,
                'sinc': success_inc,
                'finc': failure_inc,
            },
        )
        row = result.first()
        await session.commit()

        OUTCOME_RECORDED_TOTAL.labels(
            vault_id=str(vault_id), outcome='success' if success else 'failure'
        ).inc(1)

        if row is None:
            log.warning('outcome.kv_key.no_row_returned')
            return {
                'kv_key': kv_key,
                'vault_id': str(vault_uuid),
                'success_co_count': 0,
                'failure_co_count': 0,
                'last_outcome_at': None,
            }
        last_outcome_at = row[2]
        log.info(
            'outcome.kv_key.recorded',
            success_co_count=row[0],
            failure_co_count=row[1],
        )
        return {
            'kv_key': kv_key,
            'vault_id': str(vault_uuid),
            'success_co_count': row[0],
            'failure_co_count': row[1],
            'last_outcome_at': last_outcome_at.isoformat() if last_outcome_at else None,
        }
