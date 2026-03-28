"""Non-blocking audit logging service."""

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
    ) -> None:
        """Schedule an audit entry write as a background task.

        This method returns immediately — the actual DB write happens
        asynchronously.
        """
        entry = AuditLog(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            session_id=session_id,
            details=details,
        )
        asyncio.create_task(self._persist(entry))

    async def _persist(self, entry: AuditLog) -> None:
        """Persist a single audit log entry."""
        try:
            async with self._metastore.session() as session:
                session.add(entry)
                await session.commit()
        except (SQLAlchemyError, OSError, RuntimeError):
            logger.exception('Failed to write audit log entry: action=%s', entry.action)

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
