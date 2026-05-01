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
        vault_id: UUID | None = None,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> Any:
        """Flip ``MemoryUnit.is_deprioritized`` to True and record an audit event.

        ``vault_id`` scopes the mutation per Wave 0 multi-tenant invariant: if
        supplied and the unit's vault does not match, raises
        ``MemoryUnitNotFoundError`` (the route caller maps this to 404). When
        None, no vault check is applied (legacy CLI path).

        Does NOT cascade to MentalModels; does NOT call ``prune_stale_evidence``.
        The retrieval-time filter (F1b) honours the flag — see
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

    async def restore_unit(
        self,
        unit_id: UUID,
        *,
        vault_id: UUID | None = None,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> Any:
        """Flip ``MemoryUnit.is_deprioritized`` back to False (restore) and audit.

        ``vault_id`` scopes the mutation per Wave 0 multi-tenant invariant: if
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
                # Wave 0 vault-scoping invariant: cross-vault mutation rejected.
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
