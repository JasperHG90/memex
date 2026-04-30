"""Memory unit curation service — non-destructive verbs (F4)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from memex_common.exceptions import MemoryUnitNotFoundError
from memex_core.context import get_actor, get_session_id
from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.units')


class UnitsService(BaseService):
    """Memory unit curation: deprioritize / restore.

    Non-destructive counterpart to deletion (`StatsService.delete_memory_unit`).
    Per Wave 0 §6 #12: deprioritize is the NON-destructive verb; archive
    remains the destructive cleanup. They coexist.
    """

    async def set_unit_deprioritized(
        self,
        unit_id: UUID,
        reason: str,
        *,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> Any:
        """Flip ``MemoryUnit.is_deprioritized`` to True and record an audit event.

        Does NOT cascade to MentalModels; does NOT call ``prune_stale_evidence``.
        The retrieval-time filter (F1b) honours the flag — see
        ``memory/retrieval/strategies.py:93-95``.
        """
        return await self._flip_deprioritized(
            unit_id,
            value=True,
            action='memory_deprioritize',
            details={'reason': reason},
            actor=actor,
            background_tasks=background_tasks,
        )

    async def restore_unit(
        self,
        unit_id: UUID,
        *,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> Any:
        """Flip ``MemoryUnit.is_deprioritized`` back to False (restore) and audit."""
        return await self._flip_deprioritized(
            unit_id,
            value=False,
            action='memory_restore',
            details=None,
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
        actor: str | None,
        background_tasks: Any | None,
    ) -> Any:
        from memex_core.memory.sql_models import MemoryUnit

        async with self.metastore.session() as session:
            unit = await session.get(MemoryUnit, unit_id)
            if unit is None:
                raise MemoryUnitNotFoundError(f'Memory unit {unit_id} not found.')
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
