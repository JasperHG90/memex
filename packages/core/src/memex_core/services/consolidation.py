"""F38 ConsolidationService — thin orchestrator over reflection + contradiction + prune.

Per RFC-010, this is intentionally a thin module. Its **only** direct DB write
is the ``consolidation_ticks`` summary row inserted at the end of each tick
(AC-F38-4 module-write audit). All other writes are delegated.

Step ordering (B1 adjudication, AC-F38-1):
    1. ``select_diff_units`` — read AuditLog rows with ``action='outcome.record'``
       since the previous tick's ``completed_at``.
    2. ``ContradictionEngine.detect_contradictions`` — runs first so reflection
       observes the post-contradiction confidence state.
    3. ``ReflectionService.reflect_batch`` — synthesizes mental models for the
       entities touched by the diff units.
    4. ``prune_stale_evidence`` — invoked **only** on units already at
       ``ContentStatus.STALE``. F38 does NOT decide staleness — that lives in
       F25 / contradiction-driven confidence drops / archive cascade.
    5. ``write_tick_summary`` — single ``ConsolidationTick`` row per tick.

The 500-units-per-tick budget (oldest-first by ``mentioned_at`` /
``event_date``) is enforced at the ``select_diff_units`` boundary.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func as sa_func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import MemexConfig
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.reflect.models import ReflectionRequest
from memex_core.memory.sql_models import (
    AuditLog,
    ConsolidationTick,
    ContentStatus,
    MemoryUnit,
    UnitEntity,
)

from memex_core.services.locks import (
    EntityLockTimeoutError,
    acquire_entity_lock,
)
from memex_core.services.mental_model_cleanup import prune_stale_evidence
from memex_core.services.reflection import ReflectionService
from memex_core.storage.dsn import dsn_from_config
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.consolidation')

DEFAULT_TICK_BUDGET = 500

# Safety bound for the first-tick path (no prior `consolidation_ticks` row).
# Without this, `select_diff_units` would scan the entire `audit_logs` table on
# mature vaults with millions of outcome rows. One year is comfortably wider
# than any plausible reflection horizon while still keeping the scan bounded.
FIRST_TICK_LOOKBACK = timedelta(days=365)


class ConsolidationService:
    """Per-vault consolidation orchestrator.

    Owns no domain state of its own. Holds references to the metastore (for
    audit-log queries + tick-summary write), the reflection service, the
    contradiction engine, and per-step timeouts pulled from
    ``config.server.memory.consolidation``.
    """

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        config: MemexConfig,
        reflection: ReflectionService,
        contradiction: ContradictionEngine | None,
        *,
        entity_lock_timeout_seconds: float | None = None,
    ) -> None:
        self.metastore = metastore
        self.config = config
        self.reflection = reflection
        self.contradiction = contradiction
        self._dsn = dsn_from_config(config)
        # Pull the per-tick lock timeout from config so operators can tune it
        # without a redeploy. The kwarg override is kept for tests that need a
        # tighter bound; ``None`` means use the configured value.
        if entity_lock_timeout_seconds is None:
            entity_lock_timeout_seconds = (
                config.server.memory.consolidation.entity_lock_timeout_seconds
            )
        self._entity_lock_timeout_seconds = entity_lock_timeout_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def tick(
        self,
        vault_id: UUID,
        *,
        dry_run: bool = False,
        budget: int = DEFAULT_TICK_BUDGET,
    ) -> dict[str, Any]:
        """Run a single consolidation tick for one vault.

        Returns a dict with the per-step counts and the tick row id (or
        ``None`` for dry runs). On dry-run, no writes happen — neither the
        reflection/contradiction/prune calls nor the tick-summary row.
        """
        started_at = datetime.now(timezone.utc)
        last_tick = await self.get_last_tick_timestamp(vault_id)

        async with self.metastore.session() as session:
            unit_ids = await self.select_diff_units(session, vault_id, last_tick, budget=budget)
            unit_ids_by_entity = (
                await self._resolve_unit_ids_grouped_by_entity(session, unit_ids, vault_id)
                if unit_ids
                else {}
            )
            already_stale_ids = (
                await self._select_already_stale_ids(session, unit_ids, vault_id)
                if unit_ids
                else []
            )
        entity_ids: set[UUID] = set(unit_ids_by_entity.keys())
        already_stale_set = set(already_stale_ids)

        log = logger.getChild(str(vault_id))
        log.info(
            'consolidation.tick.start',
            extra={
                'vault_id': str(vault_id),
                'units': len(unit_ids),
                'entities': len(entity_ids),
                'already_stale': len(already_stale_ids),
                'dry_run': dry_run,
            },
        )

        if dry_run:
            return {
                'vault_id': str(vault_id),
                'dry_run': True,
                'units_processed': len(unit_ids),
                'entities_reflected': len(entity_ids),
                'contradictions_run': len(unit_ids),
                'stale_pruned_candidates': len(already_stale_ids),
                'entities_deferred': 0,
                'tick_id': None,
            }

        contradictions_run = 0
        entities_reflected = 0
        stale_pruned = 0
        entities_deferred: list[UUID] = []
        error: str | None = None

        # Per-entity loop under F9's per-entity advisory lock so this tick
        # cannot race with `memex_memory_reconsolidate` (LocksService) on the
        # same MentalModel/MemoryUnits. Contention policy: skip-and-log-deferred
        # — if another reconsolidation holds the lock, leave the entity for the
        # next tick rather than block the whole tick. AC-F38-1 step ordering
        # (contradiction → reflection → prune) is preserved per entity.
        for eid in entity_ids:
            entity_unit_ids = unit_ids_by_entity[eid]
            if not entity_unit_ids:
                continue
            try:
                async with acquire_entity_lock(
                    self._dsn, eid, timeout_seconds=self._entity_lock_timeout_seconds
                ):
                    try:
                        if self.contradiction is not None:
                            await self.contradiction.detect_contradictions(
                                session_factory=self.metastore.session_maker(),
                                document_id=None,
                                unit_ids=entity_unit_ids,
                                vault_id=vault_id,
                            )
                            contradictions_run += len(entity_unit_ids)
                        else:
                            log.warning('consolidation.tick.contradiction_disabled')

                        await self.reflection.reflect_batch(
                            [ReflectionRequest(entity_id=eid, vault_id=vault_id)]
                        )
                        entities_reflected += 1

                        entity_stale_ids = [
                            uid for uid in entity_unit_ids if uid in already_stale_set
                        ]
                        if entity_stale_ids:
                            async with self.metastore.session() as session:
                                await prune_stale_evidence(
                                    session,
                                    entity_ids={eid},
                                    deleted_unit_ids=entity_stale_ids,
                                    vault_id=vault_id,
                                )
                                await session.commit()
                            stale_pruned += len(entity_stale_ids)
                    except Exception as exc:
                        # Per-entity failure should not poison the tick — log
                        # and move on. Outer `error` captures the first failure
                        # for the tick-summary row (parity with prior behaviour).
                        if error is None:
                            error = f'{type(exc).__name__}: {exc}'
                        log.warning(
                            'consolidation.tick.entity_error',
                            extra={'entity_id': str(eid), 'error': str(exc)},
                            exc_info=True,
                        )
            except EntityLockTimeoutError:
                entities_deferred.append(eid)
                log.info(
                    'consolidation.tick.entity_deferred',
                    extra={
                        'entity_id': str(eid),
                        'reason': 'entity_lock_timeout',
                        'timeout_seconds': self._entity_lock_timeout_seconds,
                    },
                )
                continue

        tick_id = await self._write_tick_summary(
            vault_id=vault_id,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            units_processed=len(unit_ids),
            entities_reflected=entities_reflected,
            contradictions_run=contradictions_run,
            stale_pruned=stale_pruned,
            error=error,
        )

        log.info(
            'consolidation.tick.complete',
            extra={
                'vault_id': str(vault_id),
                'tick_id': str(tick_id),
                'error': error,
                'entities_deferred': len(entities_deferred),
            },
        )

        return {
            'vault_id': str(vault_id),
            'tick_id': str(tick_id),
            'units_processed': len(unit_ids),
            'entities_reflected': entities_reflected,
            'contradictions_run': contradictions_run,
            'stale_pruned': stale_pruned,
            'entities_deferred': len(entities_deferred),
            'error': error,
        }

    async def status(self, vault_id: UUID | None = None) -> list[dict[str, Any]]:
        """Return the most recent tick row per vault.

        Pass ``vault_id=None`` to get one row per vault (latest each); pass a
        specific vault_id to get only that vault's latest row.

        Uses Postgres ``DISTINCT ON (vault_id)`` so every vault's latest tick
        is returned regardless of total tick volume — no implicit row cap.
        """
        async with self.metastore.session() as session:
            if vault_id is not None:
                stmt = (
                    select(ConsolidationTick)
                    .where(ConsolidationTick.vault_id == vault_id)
                    .order_by(ConsolidationTick.completed_at.desc().nullslast())  # type: ignore[union-attr]
                    .limit(1)
                )
            else:
                stmt = (
                    select(ConsolidationTick)
                    .distinct(ConsolidationTick.vault_id)
                    .order_by(
                        ConsolidationTick.vault_id,
                        ConsolidationTick.completed_at.desc().nullslast(),  # type: ignore[union-attr]
                    )
                )
            result = await session.exec(stmt)
            rows = result.all()

        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    'vault_id': str(row.vault_id),
                    'started_at': row.started_at.isoformat() if row.started_at else None,
                    'completed_at': row.completed_at.isoformat() if row.completed_at else None,
                    'units_processed': row.units_processed,
                    'entities_reflected': row.entities_reflected,
                    'contradictions_run': row.contradictions_run,
                    'stale_pruned': row.stale_pruned,
                    'error': row.error,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Diff selection
    # ------------------------------------------------------------------

    async def get_last_tick_timestamp(self, vault_id: UUID) -> datetime | None:
        """Return ``MAX(completed_at)`` for the vault, or ``None`` if never run.

        Served by ``idx_consolidation_ticks_vault_completed`` (peer-review tweak).
        """
        async with self.metastore.session() as session:
            stmt = select(sa_func.max(ConsolidationTick.completed_at)).where(
                ConsolidationTick.vault_id == vault_id
            )
            result = await session.exec(stmt)  # type: ignore[call-overload]
            return result.one_or_none()

    async def select_diff_units(
        self,
        session: AsyncSession,
        vault_id: UUID,
        last_tick_timestamp: datetime | None,
        *,
        budget: int = DEFAULT_TICK_BUDGET,
    ) -> list[UUID]:
        """Return at most ``budget`` unit IDs with new outcome signal.

        Reads ``AuditLog`` rows where ``action='outcome.record'``,
        ``resource_type='memory_unit'``, ``details->>'vault_id' = vault_id``,
        and ``timestamp > last_tick_timestamp``. On the first-tick path
        (``last_tick_timestamp is None``) the lower bound falls back to
        ``now() - FIRST_TICK_LOOKBACK`` so the scan is always bounded —
        without this, mature vaults with millions of outcome rows would do a
        full ``audit_logs`` table scan (issue #98). Distinct on
        ``resource_id``; oldest-first by AuditLog.timestamp; capped to
        ``budget`` rows via SQL ``LIMIT``.

        Returns parsed UUIDs; rows whose ``resource_id`` is unparseable are
        dropped (defensive — should not happen since record_outcome writes
        ``str(uuid)``).
        """
        # Always apply a lower bound on `timestamp` to keep the audit_logs scan
        # bounded. Falls back to a 1-year window for the first-tick path.
        lower_bound = (
            last_tick_timestamp
            if last_tick_timestamp is not None
            else datetime.now(timezone.utc) - FIRST_TICK_LOOKBACK
        )
        stmt = (
            select(AuditLog.resource_id, sa_func.min(AuditLog.timestamp).label('first_seen'))
            .where(
                AuditLog.action == 'outcome.record',
                AuditLog.resource_type == 'memory_unit',
                AuditLog.details['vault_id'].astext == str(vault_id),
                AuditLog.timestamp > lower_bound,
            )
            .group_by(AuditLog.resource_id)
            .order_by(sa_func.min(AuditLog.timestamp))
            .limit(budget)
        )

        result = await session.exec(stmt)  # type: ignore[call-overload]
        out: list[UUID] = []
        for row in result.all():
            resource_id = row[0] if isinstance(row, tuple) else row.resource_id
            if not resource_id:
                continue
            try:
                out.append(UUID(resource_id))
            except (ValueError, TypeError):
                continue
            if len(out) >= budget:
                break
        return out

    async def resolve_unit_ids_to_entity_ids(
        self,
        session: AsyncSession,
        unit_ids: list[UUID],
        vault_id: UUID,
    ) -> set[UUID]:
        """Return the set of entity IDs co-occurring with any unit in ``unit_ids``.

        Vault-scoped — never crosses vault boundaries.
        """
        if not unit_ids:
            return set()
        stmt = (
            select(UnitEntity.entity_id)
            .where(UnitEntity.unit_id.in_(unit_ids), UnitEntity.vault_id == vault_id)  # type: ignore[attr-defined]
            .distinct()
        )
        result = await session.exec(stmt)
        return {row for row in result.all() if row is not None}

    async def _resolve_unit_ids_grouped_by_entity(
        self,
        session: Any,
        unit_ids: list[UUID],
        vault_id: UUID,
    ) -> dict[UUID, list[UUID]]:
        """Return a {entity_id: [unit_ids]} map for the diff units.

        Used by ``tick()`` to drive a per-entity loop under
        ``acquire_entity_lock`` so F38 cannot race with F9's
        ``memex_memory_reconsolidate`` on the same MentalModel. Vault-scoped.
        """
        if not unit_ids:
            return {}
        stmt = select(UnitEntity.entity_id, UnitEntity.unit_id).where(
            UnitEntity.unit_id.in_(unit_ids),  # type: ignore[attr-defined]
            UnitEntity.vault_id == vault_id,
        )
        result = await session.exec(stmt)
        grouped: dict[UUID, list[UUID]] = {}
        for row in result.all():
            entity_id = row[0] if isinstance(row, tuple) else row.entity_id
            unit_id = row[1] if isinstance(row, tuple) else row.unit_id
            if entity_id is None or unit_id is None:
                continue
            grouped.setdefault(entity_id, []).append(unit_id)
        return grouped

    async def _select_already_stale_ids(
        self,
        session: AsyncSession,
        unit_ids: list[UUID],
        vault_id: UUID,
    ) -> list[UUID]:
        """Return the subset of ``unit_ids`` whose ``status == STALE``.

        This is the AC-F38-2 invariant: F38 invokes ``prune_stale_evidence``
        ONLY on units already marked stale by some other service. F38 does
        NOT mutate ``status`` from active to stale.
        """
        if not unit_ids:
            return []
        stmt = select(MemoryUnit.id).where(
            MemoryUnit.id.in_(unit_ids),  # type: ignore[attr-defined]
            MemoryUnit.status == ContentStatus.STALE,
            MemoryUnit.vault_id == vault_id,
        )
        result = await session.exec(stmt)
        return [row for row in result.all() if row is not None]

    # ------------------------------------------------------------------
    # Tick-summary write (the single direct write — AC-F38-4)
    # ------------------------------------------------------------------

    async def _write_tick_summary(
        self,
        *,
        vault_id: UUID,
        started_at: datetime,
        completed_at: datetime,
        units_processed: int,
        entities_reflected: int,
        contradictions_run: int,
        stale_pruned: int,
        error: str | None,
    ) -> UUID:
        row = ConsolidationTick(
            vault_id=vault_id,
            started_at=started_at,
            completed_at=completed_at,
            units_processed=units_processed,
            entities_reflected=entities_reflected,
            contradictions_run=contradictions_run,
            stale_pruned=stale_pruned,
            error=error,
        )
        async with self.metastore.session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row.id
