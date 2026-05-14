"""Memory unit curation service — non-destructive verbs."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from memex_common.exceptions import MemoryUnitNotFoundError
from memex_common.schemas import UnitHistoryNodeDTO
from memex_core.context import get_actor, get_session_id
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
    ) -> Any:
        """Flip ``MemoryUnit.is_deprioritized`` to True and record an audit event.

        ``vault_id`` scopes the mutation per vault-scoping invariant: if
        supplied and the unit's vault does not match, raises
        ``MemoryUnitNotFoundError`` (the route caller maps this to 404). When
        None, no vault check is applied (legacy CLI path).

        Does NOT cascade to MentalModels; does NOT call ``prune_stale_evidence``.
        The retrieval-time filter honours the flag — see
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
        )

    async def batch_set_unit_deprioritized(
        self,
        unit_ids: list[UUID],
        reason: str,
        *,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> list[UUID]:
        """Flip ``is_deprioritized`` to True for many units in a single UPDATE.

        Returns the list of unit IDs whose row was actually updated (i.e.
        existed and were not already deprioritized — the predicate
        ``is_deprioritized = false`` makes the call idempotent).

        Audit emission is also batched into a single ``add_all`` INSERT so
        the per-row INSERT cost no longer dominates large consolidation
        batches. The per-row reason / cascade semantics still match the
        single-row path (one ``AuditLog`` row per affected unit).
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
            result = await session.execute(stmt)
            updated_ids = [row[0] for row in result.all()]
            await session.commit()

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
    ) -> Any:
        from memex_core.memory.sql_models import MemoryUnit

        async with self.metastore.session() as session:
            unit = await session.get(MemoryUnit, unit_id)
            if unit is None:
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
            unit.is_deprioritized = value
            session.add(unit)
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
