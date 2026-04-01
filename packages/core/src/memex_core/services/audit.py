"""Non-blocking audit logging service."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, desc
from sqlalchemy.exc import SQLAlchemyError

from memex_core.memory.sql_models import AuditLog
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.audit')


class AuditService:
    """Append-only audit logger backed by the metastore.

    Writes are fire-and-forget: they are dispatched as background tasks
    so that the hot path (request handling) is never blocked by audit I/O.
    """

    def __init__(self, metastore: AsyncBaseMetaStoreEngine) -> None:
        self._metastore = metastore

    # ------------------------------------------------------------------
    # Write (fire-and-forget)
    # ------------------------------------------------------------------

    def log(
        self,
        *,
        action: str,
        actor: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        session_id: str | None = None,
        details: dict[str, Any] | None = None,
        background_tasks: Any | None = None,
    ) -> None:
        """Schedule an audit entry write as a background task.

        This method returns immediately — the actual DB write happens
        asynchronously.  When *background_tasks* (a FastAPI ``BackgroundTasks``
        instance) is provided the work is added there; otherwise it falls back
        to ``asyncio.create_task``.
        """
        entry = AuditLog(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            session_id=session_id,
            details=details,
        )
        if background_tasks is not None:
            background_tasks.add_task(self._persist, entry)
        else:
            asyncio.create_task(self._persist(entry))

    async def _persist(self, entry: AuditLog) -> None:
        """Persist a single audit log entry."""
        try:
            async with self._metastore.session() as session:
                session.add(entry)
                await session.commit()
        except (SQLAlchemyError, OSError, RuntimeError):
            logger.exception('Failed to write audit log entry: action=%s', entry.action)
        except Exception:
            logger.exception('Unexpected error writing audit log entry: action=%s', entry.action)

    # ------------------------------------------------------------------
    # Read (query)
    # ------------------------------------------------------------------

    async def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLog]:
        """Query audit log entries with optional filters."""
        stmt = select(AuditLog)

        if actor is not None:
            stmt = stmt.where(AuditLog.actor == actor)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if resource_type is not None:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if since is not None:
            stmt = stmt.where(AuditLog.timestamp >= since)
        if until is not None:
            stmt = stmt.where(AuditLog.timestamp <= until)

        stmt = stmt.order_by(desc(AuditLog.timestamp)).limit(limit).offset(offset)

        async with self._metastore.session() as session:
            result = await session.exec(stmt)  # type: ignore[call-overload]
            return list(result.all())


def audit_event(
    audit_service: AuditService | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    **details: Any,
) -> None:
    """Emit a domain audit event. No-op when audit_service is None."""
    if audit_service is None:
        return
    from memex_core.context import get_actor, get_session_id

    audit_service.log(
        action=action,
        actor=get_actor(),
        resource_type=resource_type,
        resource_id=resource_id,
        session_id=get_session_id(),
        details=details or None,
    )
