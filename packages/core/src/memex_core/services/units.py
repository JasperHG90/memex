"""Memory unit curation service — non-destructive verbs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, cast, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.exceptions import MemoryUnitNotFoundError, ObservationReadOnlyError
from memex_common.schemas import UnitHistoryNodeDTO
from memex_core.context import get_actor, get_session_id
from memex_core.metrics import (
    DEPRIORITIZE_REJECTED_OBSERVATION_UUID_TOTAL,
    REFRESH_OBSERVATION_TASK_ENQUEUED_TOTAL,
    RESTORE_OBSERVATION_NO_AFFECTED_ENTITIES_TOTAL,
)
from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.units')

DEFAULT_HISTORY_LINK_TYPES: tuple[str, ...] = ('contradicts', 'weakens')
DEFAULT_HISTORY_MAX_DEPTH: int = 10
_SENTINEL_OLD = datetime.min.replace(tzinfo=timezone.utc)


class UnitsService(BaseService):
    """Memory unit curation: deprioritize / restore.

    Non-destructive counterpart to deletion (`StatsService.delete_memory_unit`).
    Per-unit deprioritize and note-level archive both flip
    ``MemoryUnit.is_deprioritized=true`` — neither destroys rows. The
    difference is scope: deprioritize targets a single unit;
    ``Notes.set_note_status(note_id, 'archived')`` cascades the same flip
    to every unit of the note (alongside recording ``Note.archived_at``).
    """

    async def set_unit_deprioritized(
        self,
        unit_id: UUID,
        reason: str,
        *,
        vault_id: UUID | None = None,
        actor: str | None = None,
        background_tasks: Any | None = None,
        defer_observation_refresh: bool = False,
    ) -> Any:
        """Flip ``MemoryUnit.is_deprioritized`` to True and record an audit event.

        ``vault_id`` scopes the mutation per vault-scoping invariant: if
        supplied and the unit's vault does not match, raises
        ``MemoryUnitNotFoundError`` (the route caller maps this to 404). When
        None, no vault check is applied (legacy CLI path).

        When ``unit_id`` does not match a ``MemoryUnit`` but matches an
        ``Observation.id`` inside ``mental_models.observations``, raises
        ``ObservationReadOnlyError`` carrying the underlying source MU IDs;
        the HTTP layer maps this to a structured 400 redirecting the caller.

        On a successful flip, scans ``mental_models.observations`` for
        observations citing this MU and atomically enqueues one
        ``refresh_observation`` task per match — in the same session as the
        flag flip, so rollback is real. When ``defer_observation_refresh=True``,
        the inline scan is suppressed and the caller is responsible for
        invoking ``flush_deferred_observation_refresh`` with the collected
        unit IDs after the batch boundary.

        Retrieval-time filter still honours the flag — see
        ``memory/retrieval/strategies.py:93-95``.
        """
        return await self._flip_deprioritized(
            unit_id,
            value=True,
            action='memory_deprioritize',
            details={'reason': reason},
            vault_id=vault_id,
            actor=actor,
            background_tasks=background_tasks,
            defer_observation_refresh=defer_observation_refresh,
        )

    async def batch_set_unit_deprioritized(
        self,
        unit_ids: list[UUID],
        reason: str,
        *,
        actor: str | None = None,
        background_tasks: Any | None = None,
        vault_id: UUID | None = None,
    ) -> list[UUID]:
        """Flip ``is_deprioritized`` to True for many units in a single UPDATE.

        Returns the list of unit IDs whose row was actually updated (i.e.
        existed and were not already deprioritized — the predicate
        ``is_deprioritized = false`` makes the call idempotent).

        Audit emission is also batched into a single ``add_all`` INSERT so
        the per-row INSERT cost no longer dominates large consolidation
        batches. The per-row reason / cascade semantics still match the
        single-row path (one ``AuditLog`` row per affected unit).

        After the bulk UPDATE commits, the method calls
        ``flush_deferred_observation_refresh(updated_ids, vault_id)`` to
        enqueue refresh-observation tasks per citing observation in a single
        LATERAL JSONB scan. If ``vault_id`` is None (legacy callers), the
        flush is skipped — the row is still deprio'd, but observations may
        leak until the next routine reflection cycle or reconcile tick.
        """
        from sqlalchemy import update as sa_update

        from memex_core.memory.sql_models import MemoryUnit

        if not unit_ids:
            return []

        async with self.metastore.session() as session:
            stmt = (
                sa_update(MemoryUnit)
                .where(MemoryUnit.id.in_(unit_ids))  # type: ignore[attr-defined]
                .where(MemoryUnit.is_deprioritized.is_(False))
                .values(is_deprioritized=True)
                .returning(MemoryUnit.id)
            )
            # Vault scope: when a caller supplies vault_id, the UPDATE must NOT
            # touch a unit from another vault — even if the id was passed in
            # by mistake. Cross-vault flips would corrupt vault isolation and
            # leave dangling refresh tasks on the wrong vault's mental models.
            if vault_id is not None:
                stmt = stmt.where(MemoryUnit.vault_id == vault_id)  # type: ignore[attr-defined]
            result = await session.execute(stmt)
            updated_ids = [row[0] for row in result.all()]
            await session.commit()

        if updated_ids and vault_id is not None:
            try:
                await self.flush_deferred_observation_refresh(list(updated_ids), vault_id=vault_id)
            except Exception:
                logger.exception(
                    'flush_deferred_observation_refresh failed; the reconcile-tick '
                    'pass will repair missing refresh tasks. unit_ids=%d',
                    len(updated_ids),
                )
        elif updated_ids and vault_id is None:
            # Legacy callers without a vault scope cannot trigger the vault-scoped
            # LATERAL scan; observations stay stale until the next routine reflect
            # or the reconcile-tick (vault-partitioned, opt-in for historical).
            logger.warning(
                'batch_set_unit_deprioritized: vault_id missing; %d MUs deprio`d '
                'but observation-refresh flush skipped. Observations citing these '
                'MUs will refresh on the next routine reflection cycle.',
                len(updated_ids),
            )

        if self._audit_service is not None and updated_ids:
            resolved_actor = actor if actor is not None else get_actor()
            session_id = get_session_id()
            entries = [
                {
                    'action': 'memory_deprioritize',
                    'actor': resolved_actor,
                    'resource_type': 'memory_unit',
                    'resource_id': str(uid),
                    'session_id': session_id,
                    'details': {'reason': reason},
                }
                for uid in updated_ids
            ]
            self._audit_service.log_batch(entries, background_tasks=background_tasks)
        return updated_ids

    async def restore_unit(
        self,
        unit_id: UUID,
        *,
        vault_id: UUID | None = None,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> Any:
        """Flip ``MemoryUnit.is_deprioritized`` back to False (restore) and audit.

        ``vault_id`` scopes the mutation per vault-scoping invariant: if
        supplied and the unit's vault does not match, raises
        ``MemoryUnitNotFoundError``. When None, no vault check is applied.
        """
        return await self._flip_deprioritized(
            unit_id,
            value=False,
            action='memory_restore',
            details=None,
            vault_id=vault_id,
            actor=actor,
            background_tasks=background_tasks,
        )

    async def _flip_deprioritized(
        self,
        unit_id: UUID,
        *,
        value: bool,
        action: str,
        details: dict[str, Any] | None,
        vault_id: UUID | None,
        actor: str | None,
        background_tasks: Any | None,
        defer_observation_refresh: bool = False,
    ) -> Any:
        from memex_core.memory.sql_models import MemoryUnit

        async with self.metastore.session() as session:
            unit = await session.get(MemoryUnit, unit_id)
            if unit is None:
                # Pre-resolve against MentalModel.observations — the unit_id may be an
                # Observation.id (read-only projection). If so, return 400 with
                # source_memory_units redirecting the caller to the underlying MU.
                source_mus = await self._find_source_mus_for_observation(
                    session, unit_id, vault_id=vault_id
                )
                if source_mus is not None:
                    DEPRIORITIZE_REJECTED_OBSERVATION_UUID_TOTAL.inc()
                    raise ObservationReadOnlyError(source_mus)
                raise MemoryUnitNotFoundError(f'Memory unit {unit_id} not found.')
            if vault_id is not None and unit.vault_id != vault_id:
                # Vault-scoping invariant: cross-vault mutation rejected.
                # Use 404 (not 403) so we don't leak whether the unit_id
                # exists in another vault — same disclosure stance as the
                # not-found path. The route layer's `check_vault_access` is
                # the principal-vault gate; this is the per-row scope check
                # that backstops the route when an in-scope key supplies a
                # mismatched (unit_id, vault_id) pair.
                raise MemoryUnitNotFoundError(
                    f'Memory unit {unit_id} not found in vault {vault_id}.'
                )

            # No idempotency short-circuit here: FSFM's auto-band cooldown
            # (api.py reads memory_restore audit rows within the cooldown
            # window) depends on every restore call producing an audit row.
            # Refresh-task enqueue dedupes via the partial UNIQUE; priority-
            # reflect upsert dedupes via ON CONFLICT — so a re-call is safe.
            unit.is_deprioritized = value
            session.add(unit)
            await session.flush()

            if value is True and not defer_observation_refresh:
                # Deprio path: enqueue refresh-observation tasks per citing observation
                # in the SAME session as the flag flip, so rollback rolls back both.
                await self._enqueue_refresh_tasks_for_mu(
                    session,
                    triggering_unit_id=unit_id,
                    vault_id=unit.vault_id,
                )
            elif value is False and not defer_observation_refresh:
                # Restore path: priority-lane reflect tasks per affected entity.
                await self._enqueue_priority_reflect_for_restore(
                    session,
                    restored_unit_id=unit_id,
                    vault_id=unit.vault_id,
                )

            await session.commit()
            await session.refresh(unit)

        if self._audit_service is not None:
            self._audit_service.log(
                action=action,
                actor=actor if actor is not None else get_actor(),
                resource_type='memory_unit',
                resource_id=str(unit_id),
                session_id=get_session_id(),
                details=details,
                background_tasks=background_tasks,
            )
        return unit

    async def _find_source_mus_for_observation(
        self,
        session: AsyncSession,
        observation_id: UUID,
        *,
        vault_id: UUID | None,
    ) -> list[UUID] | None:
        """If ``observation_id`` matches an Observation inside any MentalModel,
        return the list of source MU IDs from its evidence. Else return None.

        Vault-scoped when ``vault_id`` is provided. Uses the GIN index on
        ``mental_models.observations`` via ``@>`` containment.
        """
        from memex_core.memory.sql_models import MentalModel

        probe = json.dumps([{'id': str(observation_id)}])
        stmt = select(MentalModel.observations).where(
            col(MentalModel.observations).op('@>')(cast(probe, JSONB))
        )
        if vault_id is not None:
            stmt = stmt.where(col(MentalModel.vault_id) == vault_id)
        result = await session.exec(stmt)
        target = str(observation_id)
        matches: list[list[UUID]] = []
        for observations in result.all():
            for obs in observations or []:
                if not isinstance(obs, dict):
                    continue
                if str(obs.get('id')) != target:
                    continue
                ev = obs.get('evidence') or []
                source_mus: list[UUID] = []
                for ev_item in ev:
                    if not isinstance(ev_item, dict):
                        continue
                    mid = ev_item.get('memory_id')
                    if mid is None:
                        continue
                    try:
                        source_mus.append(UUID(str(mid)))
                    except (ValueError, TypeError):
                        continue
                matches.append(source_mus)
        if not matches:
            return None
        if len(matches) > 1:
            # Observation UUIDs come from uuid4 (default_factory) and should be
            # globally unique. Multiple MentalModels matching the same id is
            # invariant violation — log loudly so it doesn't go unnoticed.
            # We return the first match's source MUs (deterministic on the
            # query plan); the operator should investigate the duplicate.
            logger.error(
                'Observation %s matched %d MentalModels (UUID collision — '
                'invariant violation); returning first match only.',
                observation_id,
                len(matches),
            )
        return matches[0]

    async def _enqueue_refresh_tasks_for_mu(
        self,
        session: AsyncSession,
        *,
        triggering_unit_id: UUID,
        vault_id: UUID,
    ) -> None:
        """Scan vault-scoped MentalModels for observations citing the deprio'd MU
        and bulk-insert one ``refresh_observation`` row per (mental_model, obs)
        match. Uses ``with_for_update(of=MentalModel)`` to serialize against
        concurrent Phase 5 commits. Idempotent dedupe via partial UNIQUE.
        """
        from memex_core.memory.sql_models import MentalModel, ReflectionQueue, ReflectionStatus

        probe = json.dumps([{'evidence': [{'memory_id': str(triggering_unit_id)}]}])
        stmt = (
            select(MentalModel.id, MentalModel.entity_id, MentalModel.observations)
            .where(col(MentalModel.vault_id) == vault_id)
            .where(col(MentalModel.observations).op('@>')(cast(probe, JSONB)))
            .with_for_update(of=MentalModel)
        )
        result = await session.exec(stmt)
        rows = result.all()
        if not rows:
            return

        triggering_str = str(triggering_unit_id)
        seen: set[tuple[UUID, UUID]] = set()
        values_rows: list[dict[str, Any]] = []
        priority_lane = self.config.server.memory.reflection.refresh_obs_priority_lane
        for mm_id, entity_id, observations in rows:
            for obs in observations or []:
                if not isinstance(obs, dict):
                    continue
                ev = obs.get('evidence') or []
                if not any(
                    isinstance(e, dict) and str(e.get('memory_id')) == triggering_str for e in ev
                ):
                    continue
                obs_id_raw = obs.get('id')
                if obs_id_raw is None:
                    continue
                try:
                    obs_id = UUID(str(obs_id_raw))
                except (ValueError, TypeError):
                    continue
                key = (mm_id, obs_id)
                if key in seen:
                    continue
                seen.add(key)
                values_rows.append(
                    {
                        'entity_id': entity_id,
                        'vault_id': vault_id,
                        'status': ReflectionStatus.PENDING,
                        'priority_lane': priority_lane,
                        'priority_score': 1.0,
                        'task_type': 'refresh_observation',
                        'observation_id': obs_id,
                        'source_unit_id': triggering_unit_id,
                        'accumulated_evidence': 0,
                    }
                )
        if not values_rows:
            return
        insert_stmt = pg_insert(ReflectionQueue).values(values_rows)
        # index_where uses col(...) wrappers to match the queue_service upsert
        # form character-for-character; partial-UNIQUE arbiter inference is
        # text-normalized but consistent col() form removes future-drift risk.
        insert_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=['entity_id', 'vault_id', 'observation_id'],
            index_where=and_(
                col(ReflectionQueue.task_type) == 'refresh_observation',
                col(ReflectionQueue.status).in_(
                    [ReflectionStatus.PENDING, ReflectionStatus.PROCESSING]
                ),
            ),
        )
        # ON CONFLICT DO NOTHING returns rowcount = true insert count (conflicts
        # are silently skipped). Counting len(values_rows) would over-count
        # when a sibling deprio already enqueued the same (entity, vault, obs).
        insert_result = await session.execute(insert_stmt)
        inserted = getattr(insert_result, 'rowcount', 0) or 0
        if inserted > 0:
            REFRESH_OBSERVATION_TASK_ENQUEUED_TOTAL.inc(inserted)

    async def _enqueue_priority_reflect_for_restore(
        self,
        session: AsyncSession,
        *,
        restored_unit_id: UUID,
        vault_id: UUID,
    ) -> None:
        """Look up entities that mention the restored MU and enqueue priority-lane reflects.

        Uses ``unit_entities`` (the join table — NOT a non-existent
        ``entity_mentions``). Orphan MUs (no entity rows) emit a counter and
        return without enqueueing — the restored MU is still queryable;
        routine reflection picks it up later.
        """
        from memex_core.memory.reflect.queue_service import ReflectionQueueService
        from memex_core.memory.sql_models import UnitEntity

        stmt = (
            select(UnitEntity.entity_id)
            .where(col(UnitEntity.unit_id) == restored_unit_id)
            .where(col(UnitEntity.vault_id) == vault_id)
            .distinct()
        )
        result = await session.exec(stmt)
        entity_ids: set[UUID] = {eid for eid in result.all() if eid is not None}
        if not entity_ids:
            RESTORE_OBSERVATION_NO_AFFECTED_ENTITIES_TOTAL.inc()
            return
        queue_service = ReflectionQueueService(self.config.server.memory.reflection)
        await queue_service.enqueue_priority_reflect(session, entity_ids, vault_id)

    async def flush_deferred_observation_refresh(self, unit_ids: list[UUID], vault_id: UUID) -> int:
        """Run a vault-scoped JSONB scan + bulk INSERT of refresh-observation tasks.

        Called by callers that used ``defer_observation_refresh=True`` per-MU
        (FSFM auto-band) AND by ``batch_set_unit_deprioritized`` after its
        bulk UPDATE commits. Uses ``LATERAL unnest(:probes::jsonb[])`` so the
        GIN index fires per probe. SQLAlchemy ORM does not express LATERAL
        joins ergonomically, so this single query stays as ``text(...)``; the
        rest of the path (insert, dedupe) uses SQLModel constructs.

        Returns the number of refresh rows enqueued (after dedupe).
        """
        from memex_core.memory.sql_models import ReflectionQueue, ReflectionStatus

        if not unit_ids:
            return 0

        probes_json: list[str] = [
            json.dumps([{'evidence': [{'memory_id': str(uid)}]}]) for uid in unit_ids
        ]

        async with self.metastore.session() as session:  # type: AsyncSession
            scan = await session.execute(
                text(
                    'SELECT DISTINCT mm.id, mm.entity_id, mm.observations '
                    'FROM mental_models mm, '
                    'LATERAL unnest(CAST(:probes AS jsonb[])) AS probe(p) '
                    'WHERE mm.vault_id = :vault_id AND mm.observations @> probe.p '
                    'FOR UPDATE OF mm'
                ),
                {'vault_id': vault_id, 'probes': probes_json},
            )
            rows = scan.all()
            if not rows:
                return 0

            unit_id_strs = {str(uid) for uid in unit_ids}
            seen: set[tuple[UUID, UUID]] = set()
            values_rows: list[dict[str, Any]] = []
            priority_lane = self.config.server.memory.reflection.refresh_obs_priority_lane
            for mm_id, entity_id, observations in rows:
                for obs in observations or []:
                    if not isinstance(obs, dict):
                        continue
                    ev = obs.get('evidence') or []
                    triggering_str: str | None = None
                    for e in ev:
                        if isinstance(e, dict) and str(e.get('memory_id')) in unit_id_strs:
                            triggering_str = str(e.get('memory_id'))
                            break
                    if triggering_str is None:
                        continue
                    obs_id_raw = obs.get('id')
                    if obs_id_raw is None:
                        continue
                    try:
                        obs_id = UUID(str(obs_id_raw))
                        triggering_uuid = UUID(triggering_str)
                    except (ValueError, TypeError):
                        continue
                    key = (mm_id, obs_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    values_rows.append(
                        {
                            'entity_id': entity_id,
                            'vault_id': vault_id,
                            'status': ReflectionStatus.PENDING,
                            'priority_lane': priority_lane,
                            'priority_score': 1.0,
                            'task_type': 'refresh_observation',
                            'observation_id': obs_id,
                            'source_unit_id': triggering_uuid,
                            'accumulated_evidence': 0,
                        }
                    )
            if not values_rows:
                await session.commit()
                return 0
            insert_stmt = pg_insert(ReflectionQueue).values(values_rows)
            insert_stmt = insert_stmt.on_conflict_do_nothing(
                index_elements=['entity_id', 'vault_id', 'observation_id'],
                index_where=and_(
                    col(ReflectionQueue.task_type) == 'refresh_observation',
                    col(ReflectionQueue.status).in_(
                        [ReflectionStatus.PENDING, ReflectionStatus.PROCESSING]
                    ),
                ),
            )
            flush_result = await session.execute(insert_stmt)
            await session.commit()
            inserted = getattr(flush_result, 'rowcount', 0) or 0
            if inserted > 0:
                REFRESH_OBSERVATION_TASK_ENQUEUED_TOTAL.inc(inserted)
            return inserted

    async def get_unit_history(
        self,
        unit_id: UUID,
        *,
        max_depth: int = DEFAULT_HISTORY_MAX_DEPTH,
        vault_id: UUID | None = None,
        link_types: tuple[str, ...] = DEFAULT_HISTORY_LINK_TYPES,
    ) -> UnitHistoryNodeDTO:
        """Walk the contradiction graph backward from ``unit_id``.

        Starts at ``unit_id`` (depth=0) and recursively follows outgoing
        ``contradicts`` / ``weakens`` MemoryLink rows — i.e., links where
        ``from_unit_id == current_unit_id`` — collecting the ``to_unit_id``
        targets as predecessors. The convention follows
        ``packages/core/src/memex_core/memory/contradiction/engine.py:225-227``:
        the contradiction engine writes ``MemoryLink(from=authoritative,
        to=superseded)``, so a backward walk in time queries ``from_unit_id =
        current`` and collects ``to_unit_id`` values.

        v1 returns supersession history (negative-evidence path:
        contradicts/weakens), NOT full confidence evolution. ``reinforces``
        links are excluded because they point forward in time. A future
        ``forward=True`` extension can walk ``reinforces`` separately.

        **Cycle and DAG safety**:
        - ``visited: set[UUID]`` prevents the same node from being processed
          twice via different predecessor paths in branching DAGs (e.g.,
          A weakened by B and C, both weakened by D → D is processed once).
        - ``max_depth`` cap is the second line of defense against literal
          cycles. Nodes hit at the cap are returned with ``truncated=True``.

        **Vault scoping** (vault-scoping invariant): every link the walk
        follows must belong to ``vault_id`` (when supplied) AND match the
        starting unit's ``vault_id``. Cross-vault links are filtered out at
        query time. When ``vault_id`` is None, the unit's own ``vault_id`` is
        used (legacy CLI path).

        Returns the root ``UnitHistoryNodeDTO`` (depth=0). The branching
        structure is encoded as ``predecessors`` lists at each level. No
        reranker, no boosts, no quality filtering — graph walk is for
        completeness, not relevance. Pre-filters do NOT apply.
        """
        from memex_core.memory.sql_models import MemoryLink, MemoryUnit
        from sqlmodel import col, select

        if max_depth < 0:
            raise ValueError('max_depth must be >= 0')

        async with self.metastore.session() as session:
            root_unit = await session.get(MemoryUnit, unit_id)
            if root_unit is None:
                raise MemoryUnitNotFoundError(f'Memory unit {unit_id} not found.')
            if vault_id is not None and root_unit.vault_id != vault_id:
                raise MemoryUnitNotFoundError(
                    f'Memory unit {unit_id} not found in vault {vault_id}.'
                )

            scoped_vault_id = vault_id if vault_id is not None else root_unit.vault_id

            visited: set[UUID] = {unit_id}

            async def _walk(
                current_unit: MemoryUnit,
                current_link_type: str | None,
                current_link_metadata: dict[str, Any],
                depth: int,
            ) -> UnitHistoryNodeDTO:
                event_date = current_unit.event_date or current_unit.mentioned_at
                node = UnitHistoryNodeDTO(
                    unit_id=current_unit.id,
                    text=current_unit.text,
                    note_id=current_unit.note_id,
                    confidence=current_unit.confidence,
                    event_date=event_date,
                    link_type=current_link_type,
                    link_metadata=current_link_metadata or {},
                    depth=depth,
                    predecessors=[],
                    truncated=False,
                )

                if depth >= max_depth:
                    if not link_types:
                        return node
                    probe_stmt = (
                        select(MemoryLink.to_unit_id)
                        .where(MemoryLink.from_unit_id == current_unit.id)
                        .where(MemoryLink.vault_id == scoped_vault_id)
                        .where(col(MemoryLink.link_type).in_(list(link_types)))
                        .limit(1)
                    )
                    probe_result = await session.exec(probe_stmt)
                    if probe_result.first() is not None:
                        node.truncated = True
                    return node

                if not link_types:
                    return node

                link_stmt = (
                    select(MemoryLink)
                    .where(MemoryLink.from_unit_id == current_unit.id)
                    .where(MemoryLink.vault_id == scoped_vault_id)
                    .where(col(MemoryLink.link_type).in_(list(link_types)))
                )
                link_result = await session.exec(link_stmt)
                outgoing_links = list(link_result.all())

                predecessor_ids = [lnk.to_unit_id for lnk in outgoing_links]
                fresh_ids: list[UUID] = []
                fresh_links: list[MemoryLink] = []
                for lnk in outgoing_links:
                    if lnk.to_unit_id in visited:
                        continue
                    visited.add(lnk.to_unit_id)
                    fresh_ids.append(lnk.to_unit_id)
                    fresh_links.append(lnk)

                if not fresh_ids:
                    if predecessor_ids:
                        node.truncated = True
                    return node

                pred_stmt = (
                    select(MemoryUnit)
                    .where(col(MemoryUnit.id).in_(fresh_ids))
                    .where(MemoryUnit.vault_id == scoped_vault_id)
                )
                pred_result = await session.exec(pred_stmt)
                pred_units = {u.id: u for u in pred_result.all()}

                children: list[UnitHistoryNodeDTO] = []
                for lnk in fresh_links:
                    pred_unit = pred_units.get(lnk.to_unit_id)
                    if pred_unit is None:
                        logger.warning(
                            'cross-vault link in vault %s: link.from=%s link.to=%s '
                            '— predecessor unit not in vault',
                            scoped_vault_id,
                            lnk.from_unit_id,
                            lnk.to_unit_id,
                        )
                        node.truncated = True
                        continue
                    child = await _walk(
                        pred_unit,
                        current_link_type=lnk.link_type,
                        current_link_metadata=dict(lnk.link_metadata or {}),
                        depth=depth + 1,
                    )
                    children.append(child)

                children.sort(
                    key=lambda c: (
                        c.event_date or _SENTINEL_OLD,
                        str(c.unit_id),
                    )
                )
                node.predecessors = children
                return node

            root = await _walk(
                root_unit,
                current_link_type=None,
                current_link_metadata={},
                depth=0,
            )

        return root
