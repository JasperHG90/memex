"""Revisitation service — eligibility, scheduling, listing, review.

Owns the revisit-policy domain:
- 5-gate eligibility predicate (Python + SQL forms — single source of truth)
- populate_initial_schedules: seed `revisit_due_at` for never-evaluated units
- list_due: vault-scoped due-list with eligibility re-applied at query time
- review: schedule advance + outcome record + sticky-streak update + audit

Algorithm: FSRS-5 via `memex_core.memory.revisit` (thin wrapper over py-fsrs).
Counter mutation discipline: `success_co_count` / `failure_co_count` are NEVER
written here — only `OutcomeService.record_outcome` writes those. The grep
audit at `tests/integration/test_int_f20_review.py` enforces that discipline
at the module-source level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from memex_core.memory.revisit import Quality, UnitState, schedule
from memex_core.memory.sql_models import MemoryUnit
from memex_core.services.audit import audit_event
from memex_core.services.base import BaseService
from memex_core.services.outcomes import OutcomeService, compute_mw_score

CONFIDENCE_FLOOR_DEFAULT = 0.5
MW_THRESHOLD_DEFAULT = 0.4
STICKY_AGAIN_THRESHOLD = 5


def is_eligible_for_review(
    unit: MemoryUnit,
    *,
    confidence_floor: float = CONFIDENCE_FLOOR_DEFAULT,
    mw_threshold: float = MW_THRESHOLD_DEFAULT,
) -> bool:
    """5-gate eligibility predicate — all gates must pass.

    Reuses `compute_mw_score` directly so the Python policy fn and the
    SQL listing query share a single source of truth on the formula.
    """
    if unit.intent_class not in ('permanent', 'durable'):
        return False
    if unit.status != 'active':
        return False
    if unit.is_deprioritized:
        return False
    if unit.confidence < confidence_floor:
        return False
    mw = compute_mw_score(unit.success_co_count, unit.failure_co_count)
    return mw >= mw_threshold


def eligibility_where_clause(
    *,
    confidence_floor: float = CONFIDENCE_FLOOR_DEFAULT,
    mw_threshold: float = MW_THRESHOLD_DEFAULT,
) -> sa.sql.ClauseElement:
    """SQL form of the 5-gate predicate.

    The mw_score subexpression mirrors `compute_mw_score(s, f)` verbatim:
        (success_co_count + 1.0) / (success_co_count + failure_co_count + 2.0)
    Beta-Bernoulli α=β=1 prior; cold-start (0/0) yields 0.5.
    """
    return sa.and_(
        MemoryUnit.intent_class.in_(('permanent', 'durable')),
        MemoryUnit.status == 'active',
        MemoryUnit.is_deprioritized.is_(False),
        MemoryUnit.confidence >= confidence_floor,
        (
            (sa.cast(MemoryUnit.success_co_count, sa.Float) + 1.0)
            / (
                sa.cast(MemoryUnit.success_co_count, sa.Float)
                + sa.cast(MemoryUnit.failure_co_count, sa.Float)
                + 2.0
            )
        )
        >= mw_threshold,
    )


@dataclass(frozen=True)
class DueUnit:
    unit_id: UUID
    text_preview: str
    revisit_due_at: datetime
    intent_class: str


class RevisitationService(BaseService):
    """Revisit-policy verb-level service.

    Inherits the standard `(metastore, filestore, config)` triple from
    `BaseService`; the audit service is wired post-construction by the
    MemexAPI facade (see `api.py`).
    """

    async def populate_initial_schedules(self, vault_id: UUID) -> int:
        """Seed `revisit_due_at` for never-evaluated eligible units.

        Idempotent in two senses:
          (1) A unit that already has `revisit_due_at IS NOT NULL` is skipped.
          (2) A unit that was previously evaluated and excluded by the
              eligibility predicate is NOT retroactively scheduled here.
              Re-eligibility flows via the daily scheduler tick, not this
              populator.

        Returns the number of newly-scheduled units.
        """
        now = datetime.now(timezone.utc)
        async with self.metastore.session() as session:
            stmt = sa.select(MemoryUnit).where(
                MemoryUnit.vault_id == vault_id,
                MemoryUnit.revisit_due_at.is_(None),
                eligibility_where_clause(),
            )
            result = await session.exec(stmt)  # type: ignore[call-overload]
            scheduled = 0
            for unit in result.scalars().all():
                next_due, _interval, stability, difficulty = schedule(
                    state=None,
                    quality=Quality.GOOD,
                    now=now,
                )
                unit.revisit_due_at = next_due
                unit.revisit_stability = stability
                unit.revisit_difficulty = difficulty
                unit.revisit_last_reviewed_at = now
                session.add(unit)
                scheduled += 1
            await session.commit()
        return scheduled

    async def list_due(
        self,
        vault_id: UUID,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[DueUnit]:
        """Return units with `revisit_due_at <= now()` in this vault.

        The eligibility predicate is applied AGAIN at query time — a unit
        scheduled before becoming deprioritized (or stale, or low-confidence)
        is filtered out at listing time (predicate enforced at both seed-time
        and query-time). This protects against split-brain between scheduling
        and serving.
        """
        cutoff = now or datetime.now(timezone.utc)
        async with self.metastore.session() as session:
            stmt = (
                sa.select(MemoryUnit)
                .where(
                    MemoryUnit.vault_id == vault_id,
                    MemoryUnit.revisit_due_at.is_not(None),
                    MemoryUnit.revisit_due_at <= cutoff,
                    eligibility_where_clause(),
                )
                .order_by(MemoryUnit.revisit_due_at.asc())
                .limit(limit)
            )
            result = await session.exec(stmt)  # type: ignore[call-overload]
            return [
                DueUnit(
                    unit_id=unit.id,
                    text_preview=(unit.text or '')[:200],
                    revisit_due_at=unit.revisit_due_at,
                    intent_class=unit.intent_class,
                )
                for unit in result.scalars().all()
            ]

    async def review(
        self,
        unit_id: UUID,
        quality: Quality,
        *,
        vault_id: UUID,
        actor: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Advance schedule + record outcome + maintain sticky streak + audit.

        Sticky semantics:
        - `revisit_review_count` tracks **consecutive** Again ratings since
          the last non-Again. Resets to 0 on any Hard/Good/Easy rating.
        - When the count reaches `STICKY_AGAIN_THRESHOLD` (5), this method
          flips `is_deprioritized=true` automatically.
        - The unset path is intentionally NOT here — once deprioritized,
          positive outcomes never auto-restore. Only `memex_memory_restore`
          can flip `is_deprioritized` back to false.

        Quality mapping:
          quality ∈ {AGAIN, HARD}  → record_outcome(success=False)
          quality ∈ {GOOD, EASY}   → record_outcome(success=True)

        Order of operations (single transaction):
          1. Load + row-lock the unit
          2. Assert unit.vault_id matches the caller's vault —
             vault-scoping invariant; cross-vault review is rejected.
          3. FSRS-5 schedule advance via `memory.revisit.schedule()`
          4. Persist new schedule + sticky streak + (maybe) is_deprioritized
          5. OutcomeService.record_outcome — atomic counter increments
          6. audit_event('memory_review', ...)
          7. Single commit

        Cross-feature plumbing (consolidation hook):
        Step 5 inserts an audit row that `select_diff_units` observes
        on its next consolidation tick — so a review automatically schedules
        the unit for downstream contradiction + reflection passes without any
        direct service coupling. The audit-log signal IS the integration seam.
        """
        review_at = now or datetime.now(timezone.utc)
        # OutcomeService is intentionally stateless: it has no metastore/filestore
        # dependency and accepts the session per call (see services/outcomes.py).
        # The no-arg constructor is correct here, not a missing-deps bug.
        outcome_service = OutcomeService()
        async with self.metastore.session() as session:
            unit = await session.get(MemoryUnit, unit_id, with_for_update=True)
            if unit is None:
                raise ValueError(f'memory unit not found: {unit_id}')
            if unit.vault_id != vault_id:
                raise PermissionError(f'memory unit {unit_id} does not belong to vault {vault_id}')

            prior_state: UnitState | None
            if unit.revisit_stability is None or unit.revisit_difficulty is None:
                prior_state = None
            else:
                # Prefer the dedicated last-reviewed timestamp; fall back to
                # revisit_due_at for legacy rows from migration 026 that
                # predate the 030 column. New reviews always populate
                # revisit_last_reviewed_at, so the fallback is one-shot.
                last_reviewed = unit.revisit_last_reviewed_at or unit.revisit_due_at
                prior_state = UnitState(
                    stability=unit.revisit_stability,
                    difficulty=unit.revisit_difficulty,
                    last_review=last_reviewed,
                )

            next_due, interval_days, new_stab, new_diff = schedule(
                state=prior_state,
                quality=quality,
                now=review_at,
            )

            unit.revisit_due_at = next_due
            unit.revisit_stability = new_stab
            unit.revisit_difficulty = new_diff
            unit.revisit_last_reviewed_at = review_at

            if quality == Quality.AGAIN:
                unit.revisit_review_count = unit.revisit_review_count + 1
            else:
                unit.revisit_review_count = 0

            auto_deprioritized = False
            if not unit.is_deprioritized and unit.revisit_review_count >= STICKY_AGAIN_THRESHOLD:
                unit.is_deprioritized = True
                auto_deprioritized = True

            session.add(unit)
            await session.flush()

            success = quality in (Quality.GOOD, Quality.EASY)
            await outcome_service.record_outcome(
                session=session,
                unit_ids=[str(unit_id)],
                success=success,
                vault_id=str(unit.vault_id),
                reason=f'memory_review:{quality.name.lower()}',
            )

            audit_event(
                self._audit_service,
                action='memory_review',
                resource_type='memory_unit',
                resource_id=str(unit_id),
                quality=quality.name.lower(),
                next_review_at=next_due.isoformat(),
                interval_days=interval_days,
                review_count=unit.revisit_review_count,
                auto_deprioritized=auto_deprioritized,
                vault_id=str(unit.vault_id),
            )

            await session.commit()

        return {
            'unit_id': str(unit_id),
            'quality': quality.name.lower(),
            'next_review_at': next_due.isoformat(),
            'interval_days': interval_days,
            'review_count': unit.revisit_review_count,
            'auto_deprioritized': auto_deprioritized,
        }
