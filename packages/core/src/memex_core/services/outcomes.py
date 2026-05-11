"""Outcome recording service for Memory Worth counters.

Exposes `record_outcome()` for incrementing success/failure/unused
co-occurrence counters on MemoryUnit, UnitEntity, and MentalModel, and
`compute_mw_score() / compute_mw_boost()` for the Beta-Bernoulli posterior
mean used by the retrieval composition.

The Memory Worth formula uses additive-marginal composition:
    mw_score = (success_co_count + 1) / (success_co_count + failure_co_count + 2)
    mw_boost = 1.0 + mw_alpha * (mw_score - 0.5)

Cold-start units (0/0) get mw_score = 0.5 → mw_boost = 1.0 (neutral).
"""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlmodel import select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.retrieval.mw_ema import compute_mw_ema_score
from memex_core.memory.sql_models import MWMode, Vault
from memex_core.metrics import (
    MW_SCORE_DISTRIBUTION,
    OUTCOME_COVERAGE_RATIO,
    OUTCOME_RECORDED_TOTAL,
    OUTCOME_VERB_TOTAL,
)

logger = structlog.get_logger(__name__)

UnitOutcomeVerb = Literal['helpful', 'not_helpful', 'not_used']
CREDIT_BEARING_VERBS: frozenset[str] = frozenset({'helpful', 'not_helpful'})


class UnitOutcome(BaseModel):
    """Per-unit outcome verb with a free-text reason.

    Verb taxonomy:
      * ``helpful`` — unit contributed to a successful outcome (success_co bump).
      * ``not_helpful`` — unit was misleading or wrong (failure_co bump).
      * ``not_used`` — unit was retrieved but the caller did not use it
        (unused_co bump; engagement-only signal).

    ``reason`` is REQUIRED for credit-bearing verbs (``helpful`` /
    ``not_helpful``) so the audit log carries enough signal to detect
    rich-get-richer dynamics; OPTIONAL for ``not_used``.
    """

    unit_id: UUID = Field(..., description='UUID of the memory unit.')
    verb: UnitOutcomeVerb = Field(..., description='Per-unit verb classification.')
    reason: str | None = Field(default=None, description='Free-text reason (≤ 200 chars).')

    @model_validator(mode='after')
    def _reason_required_for_credit(self) -> 'UnitOutcome':
        if self.verb in CREDIT_BEARING_VERBS and not (self.reason and self.reason.strip()):
            raise ValueError(f"UnitOutcome with verb={self.verb!r} requires a non-empty 'reason'")
        return self


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
    *,
    mw_mode: MWMode = MWMode.STATIONARY,
    last_outcome_at: datetime | None = None,
    half_life_days: float = 60.0,
    now: datetime | None = None,
) -> float:
    """Additive-marginal Memory Worth boost factor for retrieval composition.

    Returns 1.0 for cold-start units (neutral — no rank change).

    When mw_mode is 'ema', the counters are EMA-decayed before computing the
    posterior, so old evidence fades toward the prior mean of 0.5.
    """
    if mw_mode == MWMode.EMA:
        resolved_now = now or datetime.now(timezone.utc)
        mw_score = compute_mw_ema_score(
            success=success_co_count,
            failure=failure_co_count,
            last_outcome_at=last_outcome_at,
            half_life_days=half_life_days,
            now=resolved_now,
        )
    else:
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
        unit_ids: list[str] | None = None,
        success: bool | None = None,
        vault_id: str | None = None,
        outcome_confidence: float = 1.0,
        reason: str | None = None,
        *,
        target_type: str = 'memory_unit',
        kv_key: str | None = None,
        mw_ema_half_life_days: float = 60.0,
        units: list[UnitOutcome] | list[dict[str, Any]] | None = None,
        caller_id: str | None = None,
        turn_outcome: str | None = None,
        retrieved_set_size: int | None = None,
        exploration_tagged: bool = False,
        coverage_check_mode: Literal['strict', 'permissive'] = 'permissive',
    ) -> dict[str, Any]:
        """Record an outcome.

        Two target modes (selected by keyword-only ``target_type``):

        * ``target_type='memory_unit'`` (default): increments
          success/failure/unused co-counters on each MemoryUnit and propagates
          success/failure to linked UnitEntity + MentalModel rows. Accepts
          either the per-unit ``units=[UnitOutcome, ...]`` shape (preferred)
          or the legacy ``(unit_ids, success)`` shape (FutureWarning).
        * ``target_type='kv_key'`` (procedure outcomes): increments
          success/failure co-counters on the vault-scoped
          ``procedure_outcomes`` row matching ``(vault_id, kv_key)``. KV mode
          still uses the binary ``success`` parameter.

        Returns:
            Dict with counts of updated rows. For ``memory_unit``:
            ``{units_updated, entities_updated, models_updated, audit_log_id,
            verb_counts, coverage_ratio}``. For ``kv_key``: ``{kv_key,
            vault_id, success_co_count, failure_co_count, last_outcome_at}``.
        """
        if outcome_confidence is not None and outcome_confidence != 1.0:
            warnings.warn(
                'outcome_confidence is currently ignored in counter arithmetic '
                '(v1 uses integer increments). Fractional weighting is tracked '
                'as a future enhancement. The supplied value '
                f'(outcome_confidence={outcome_confidence!r}) will be logged '
                'but not affect counters.',
                FutureWarning,
                stacklevel=2,
            )
        if target_type == 'kv_key':
            if units or unit_ids:
                raise ValueError(
                    'kv_key mode does not accept unit_ids or units; pass them only '
                    'with target_type="memory_unit"'
                )
            if success is None:
                raise ValueError("kv_key mode requires the 'success' boolean.")
            return await self._record_outcome_kv_key(
                session=session,
                kv_key=kv_key,
                success=success,
                vault_id=vault_id,
                reason=reason,
            )
        if target_type != 'memory_unit':
            raise ValueError(f"target_type must be 'memory_unit' or 'kv_key', got {target_type!r}")
        if vault_id is None:
            raise ValueError("memory_unit mode requires 'vault_id'.")

        resolved_units = self._resolve_unit_outcomes(
            units=units,
            unit_ids=unit_ids,
            success=success,
            reason=reason,
        )

        if retrieved_set_size is not None and retrieved_set_size > 0:
            coverage_ratio = min(1.0, len(resolved_units) / retrieved_set_size)
        else:
            coverage_ratio = None

        if coverage_check_mode == 'strict':
            if retrieved_set_size is not None and len(resolved_units) < retrieved_set_size:
                raise ValueError(
                    'Strict coverage: reported '
                    f'{len(resolved_units)} of {retrieved_set_size} retrieved units. '
                    'Classify every retrieved unit or switch coverage_check_mode to permissive.'
                )

        try:
            vault_uuid = UUID(vault_id)
        except ValueError as exc:
            raise ValueError(f'Invalid vault_id: {vault_id}') from exc

        helpful_ids: list[UUID] = []
        not_helpful_ids: list[UUID] = []
        not_used_ids: list[UUID] = []
        for uo in resolved_units:
            if uo.verb == 'helpful':
                helpful_ids.append(uo.unit_id)
            elif uo.verb == 'not_helpful':
                not_helpful_ids.append(uo.unit_id)
            else:
                not_used_ids.append(uo.unit_id)

        log = logger.bind(
            unit_count=len(resolved_units),
            vault_id=str(vault_id),
            confidence=outcome_confidence,
        )
        log.info(
            'outcome.record',
            helpful=len(helpful_ids),
            not_helpful=len(not_helpful_ids),
            not_used=len(not_used_ids),
        )

        if not resolved_units:
            log.warning('outcome.no_valid_ids')
            return {
                'units_updated': 0,
                'entities_updated': 0,
                'models_updated': 0,
                'audit_log_id': None,
                'verb_counts': {'helpful': 0, 'not_helpful': 0, 'not_used': 0},
                'coverage_ratio': coverage_ratio,
            }

        from memex_core.memory.sql_models import (
            AuditLog,
            MemoryUnit as MU,
            MentalModel as MM,
            OutcomeAuditLog,
            UnitEntity as UE,
        )

        now = datetime.now(timezone.utc)

        async def _bump_counter(
            ids: list[UUID],
            counter_field: str,
            *,
            bump_last_outcome_at: bool,
            propagate_to_entity_model: bool,
        ) -> tuple[int, int, int]:
            if not ids:
                return 0, 0, 0
            values: dict[str, Any] = {
                counter_field: MU.__table__.c[counter_field] + 1,
            }
            if bump_last_outcome_at:
                values['last_outcome_at'] = now
            mu_result = await session.exec(
                update(MU).where(MU.id.in_(ids), MU.vault_id == vault_uuid).values(values)
            )
            mu_count = mu_result.rowcount  # type: ignore[union-attr]
            entity_count = 0
            model_count = 0
            if propagate_to_entity_model:
                entity_result = await session.exec(
                    update(UE)
                    .where(UE.unit_id.in_(ids), UE.vault_id == vault_uuid)
                    .values({counter_field: UE.__table__.c[counter_field] + 1})
                )
                entity_count = entity_result.rowcount  # type: ignore[union-attr]
                model_result = await session.exec(
                    update(MM)
                    .where(
                        MM.entity_id.in_(
                            select(UE.entity_id).where(
                                UE.unit_id.in_(ids), UE.vault_id == vault_uuid
                            )
                        ),
                        MM.vault_id == vault_uuid,
                    )
                    .values({counter_field: MM.__table__.c[counter_field] + 1})
                )
                model_count = model_result.rowcount  # type: ignore[union-attr]
            else:
                ue_result = await session.exec(
                    update(UE)
                    .where(UE.unit_id.in_(ids), UE.vault_id == vault_uuid)
                    .values({counter_field: UE.__table__.c[counter_field] + 1})
                )
                entity_count = ue_result.rowcount  # type: ignore[union-attr]
                mm_result = await session.exec(
                    update(MM)
                    .where(
                        MM.entity_id.in_(
                            select(UE.entity_id).where(
                                UE.unit_id.in_(ids), UE.vault_id == vault_uuid
                            )
                        ),
                        MM.vault_id == vault_uuid,
                    )
                    .values({counter_field: MM.__table__.c[counter_field] + 1})
                )
                model_count = mm_result.rowcount  # type: ignore[union-attr]
            return mu_count, entity_count, model_count

        h_units, h_entities, h_models = await _bump_counter(
            helpful_ids,
            'success_co_count',
            bump_last_outcome_at=True,
            propagate_to_entity_model=True,
        )
        nh_units, nh_entities, nh_models = await _bump_counter(
            not_helpful_ids,
            'failure_co_count',
            bump_last_outcome_at=True,
            propagate_to_entity_model=True,
        )
        # not_used does NOT bump last_outcome_at — engagement-only signal,
        # must not reset the Beta-Bernoulli decay clock.
        nu_units, nu_entities, nu_models = await _bump_counter(
            not_used_ids,
            'unused_co_count',
            bump_last_outcome_at=False,
            propagate_to_entity_model=True,
        )

        units_updated = h_units + nh_units + nu_units
        entity_count = h_entities + nh_entities + nu_entities
        model_count = h_models + nh_models + nu_models

        # Diff hook: emit one AuditLog row per credit-bearing unit so the
        # consolidation tick still picks up new outcome signal. not_used is
        # excluded — it is engagement metadata, not a Memory Worth signal.
        for uo in resolved_units:
            if uo.verb == 'not_used':
                continue
            session.add(
                AuditLog(
                    action='outcome.record',
                    resource_type='memory_unit',
                    resource_id=str(uo.unit_id),
                    details={
                        'vault_id': str(vault_uuid),
                        'verb': uo.verb,
                        'outcome': 'success' if uo.verb == 'helpful' else 'failure',
                    },
                )
            )

        audit_row = OutcomeAuditLog(
            vault_id=vault_uuid,
            caller_id=caller_id,
            units=[
                {'unit_id': str(uo.unit_id), 'verb': uo.verb, 'reason': uo.reason}
                for uo in resolved_units
            ],
            turn_outcome=turn_outcome,
            retrieved_set_size=retrieved_set_size,
            coverage_ratio=coverage_ratio,
            exploration_tagged=exploration_tagged,
        )
        session.add(audit_row)

        # Single commit covers all counter updates + audit rows so the
        # co-occurrence invariant across MemoryUnit / UnitEntity / MentalModel
        # holds atomically.
        await session.commit()
        await session.refresh(audit_row)

        # Observe Memory Worth scores post-commit (read-only).
        credit_ids = helpful_ids + not_helpful_ids
        if credit_ids:
            refreshed = await session.exec(
                select(MU).where(MU.id.in_(credit_ids), MU.vault_id == vault_uuid)
            )
            vault_row = await session.get(Vault, vault_uuid)
            mw_mode_val = vault_row.mw_mode if vault_row else MWMode.STATIONARY
            for unit in refreshed.all():
                if mw_mode_val == MWMode.EMA:
                    mw_score = compute_mw_ema_score(
                        unit.success_co_count,
                        unit.failure_co_count,
                        unit.last_outcome_at,
                        half_life_days=mw_ema_half_life_days,
                        now=now,
                    )
                else:
                    mw_score = compute_mw_score(unit.success_co_count, unit.failure_co_count)
                MW_SCORE_DISTRIBUTION.labels(vault_id=str(vault_id), mode=mw_mode_val).observe(
                    mw_score
                )

        if h_units:
            OUTCOME_RECORDED_TOTAL.labels(vault_id=str(vault_id), outcome='success').inc(h_units)
        if nh_units:
            OUTCOME_RECORDED_TOTAL.labels(vault_id=str(vault_id), outcome='failure').inc(nh_units)
        for verb, count in (
            ('helpful', len(helpful_ids)),
            ('not_helpful', len(not_helpful_ids)),
            ('not_used', len(not_used_ids)),
        ):
            if count:
                OUTCOME_VERB_TOTAL.labels(vault_id=str(vault_id), verb=verb).inc(count)
        if coverage_ratio is not None:
            OUTCOME_COVERAGE_RATIO.labels(vault_id=str(vault_id), mode=coverage_check_mode).observe(
                coverage_ratio
            )

        log.info(
            'outcome.recorded',
            units_updated=units_updated,
            entities_updated=entity_count,
            models_updated=model_count,
            verb_counts={
                'helpful': len(helpful_ids),
                'not_helpful': len(not_helpful_ids),
                'not_used': len(not_used_ids),
            },
            coverage_ratio=coverage_ratio,
        )

        return {
            'units_updated': units_updated,
            'entities_updated': entity_count,
            'models_updated': model_count,
            'audit_log_id': str(audit_row.id) if audit_row.id else None,
            'verb_counts': {
                'helpful': len(helpful_ids),
                'not_helpful': len(not_helpful_ids),
                'not_used': len(not_used_ids),
            },
            'coverage_ratio': coverage_ratio,
        }

    @staticmethod
    def _resolve_unit_outcomes(
        *,
        units: list[UnitOutcome] | list[dict[str, Any]] | None,
        unit_ids: list[str] | None,
        success: bool | None,
        reason: str | None,
    ) -> list[UnitOutcome]:
        """Normalise inputs to a `list[UnitOutcome]`.

        Accepts:
          * ``units`` — preferred; per-unit verbs.
          * Legacy ``unit_ids`` + ``success`` — emits FutureWarning and
            translates to ``UnitOutcome(verb='helpful' | 'not_helpful')``.
        """
        if units is not None and unit_ids:
            raise ValueError(
                "Pass either 'units' (preferred) or legacy ('unit_ids' + 'success'), not both."
            )

        resolved: list[UnitOutcome] = []
        if units is not None:
            for item in units:
                if isinstance(item, UnitOutcome):
                    resolved.append(item)
                elif isinstance(item, dict):
                    resolved.append(UnitOutcome.model_validate(item))
                else:
                    raise ValueError(f'units entries must be UnitOutcome or dict, got {type(item)}')
            return resolved

        if unit_ids is None and success is None:
            return []

        if success is None:
            raise ValueError(
                "Legacy shape requires 'success' alongside 'unit_ids'. "
                "Prefer passing 'units=[UnitOutcome(...)]' instead."
            )
        warnings.warn(
            'record_outcome called with the legacy (unit_ids, success) shape. '
            'Switch to units=[UnitOutcome(unit_id=..., verb=..., reason=...)]; '
            'the legacy shape will be removed in a future release.',
            FutureWarning,
            stacklevel=3,
        )
        verb: UnitOutcomeVerb = 'helpful' if success else 'not_helpful'
        legacy_reason = reason or (
            'Legacy success outcome' if success else 'Legacy failure outcome'
        )
        for uid_str in unit_ids or []:
            try:
                parsed = UUID(uid_str)
            except (ValueError, TypeError):
                logger.warning('outcome.invalid_unit_id', unit_id=uid_str)
                continue
            resolved.append(UnitOutcome(unit_id=parsed, verb=verb, reason=legacy_reason))
        return resolved

    async def _record_outcome_kv_key(
        self,
        session: AsyncSession,
        kv_key: str | None,
        success: bool,
        vault_id: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        """Vault-scoped Memory Worth counter increment for a procedure KV key.

        Upserts into ``procedure_outcomes`` keyed on ``(vault_id, kv_key)``,
        atomically incrementing ``success_co_count`` (success=True) or
        ``failure_co_count`` (success=False) and stamping
        ``last_outcome_at = now()``.

        Validates that ``kv_key`` matches the
        ``procedure:<verb>:<context-tag>`` shape before touching the DB.
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
        # Rely on the FK constraint to ``kv_entries.key`` to validate that
        # the referenced procedure exists; catching the raw IntegrityError
        # here avoids the TOCTOU race a SELECT 1 pre-check would introduce.
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
        try:
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
        except IntegrityError as exc:
            await session.rollback()
            err_text = str(exc).lower()
            if 'foreign key' in err_text or 'fk_' in err_text:
                raise ValueError(f'Cannot record outcome for unknown kv_key: {kv_key!r}') from exc
            raise

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
