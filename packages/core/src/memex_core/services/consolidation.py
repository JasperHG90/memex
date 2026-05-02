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
from datetime import datetime, timezone
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
from memex_core.services.mental_model_cleanup import prune_stale_evidence
from memex_core.services.reflection import ReflectionService
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.consolidation')

DEFAULT_TICK_BUDGET = 500


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
    ) -> None:
        self.metastore = metastore
        self.config = config
        self.reflection = reflection
        self.contradiction = contradiction

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
            entity_ids = (
                await self.resolve_unit_ids_to_entity_ids(session, unit_ids, vault_id)
                if unit_ids
                else set()
            )
            already_stale_ids = (
                await self._select_already_stale_ids(session, unit_ids, vault_id)
                if unit_ids
                else []
            )

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
                'stale_pruned': len(already_stale_ids),
                'tick_id': None,
            }

        contradictions_run = 0
        entities_reflected = 0
        stale_pruned = 0
        error: str | None = None

        if unit_ids:
            try:
                # Step 2: contradiction BEFORE reflection (B1 adjudication).
                if self.contradiction is not None:
                    await self.contradiction.detect_contradictions(
                        session_factory=self.metastore.session_maker(),
                        document_id=None,
                        unit_ids=unit_ids,
                        vault_id=vault_id,
                    )
                    contradictions_run = len(unit_ids)
                else:
                    log.warning('consolidation.tick.contradiction_disabled')

                # Step 3: reflection AFTER contradiction.
                if entity_ids:
                    requests = [
                        ReflectionRequest(entity_id=eid, vault_id=vault_id) for eid in entity_ids
                    ]
                    await self.reflection.reflect_batch(requests)
                    entities_reflected = len(entity_ids)

                # Step 4: prune ONLY units already STALE (F38 does NOT stale).
                if already_stale_ids and entity_ids:
                    async with self.metastore.session() as session:
                        await prune_stale_evidence(
                            session,
                            entity_ids=entity_ids,
                            deleted_unit_ids=already_stale_ids,
                            vault_id=vault_id,
                        )
                        await session.commit()
                    stale_pruned = len(already_stale_ids)
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
                log.warning('consolidation.tick.error', extra={'error': error}, exc_info=True)

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
            },
        )

        return {
            'vault_id': str(vault_id),
            'tick_id': str(tick_id),
            'units_processed': len(unit_ids),
            'entities_reflected': entities_reflected,
            'contradictions_run': contradictions_run,
            'stale_pruned': stale_pruned,
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
        and ``timestamp > last_tick_timestamp`` (or all rows if no prior tick).
        Distinct on ``resource_id``; oldest-first by AuditLog.timestamp;
        capped to ``budget`` rows via SQL ``LIMIT``.

        Returns parsed UUIDs; rows whose ``resource_id`` is unparseable are
        dropped (defensive — should not happen since record_outcome writes
        ``str(uuid)``).
        """
        stmt = (
            select(AuditLog.resource_id, sa_func.min(AuditLog.timestamp).label('first_seen'))
            .where(
                AuditLog.action == 'outcome.record',
                AuditLog.resource_type == 'memory_unit',
                AuditLog.details['vault_id'].astext == str(vault_id),
            )
            .group_by(AuditLog.resource_id)
            .order_by(sa_func.min(AuditLog.timestamp))
            .limit(budget)
        )
        if last_tick_timestamp is not None:
            stmt = stmt.where(AuditLog.timestamp > last_tick_timestamp)

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
