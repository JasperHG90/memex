"""Audit log query endpoints."""

import logging
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from memex_core.server.auth import require_admin_auth
from memex_core.services.audit import AuditService

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1/admin', dependencies=[Depends(require_admin_auth)])


class AuditEntryDTO(BaseModel):
    id: UUID
    timestamp: datetime
    actor: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    session_id: str | None
    details: dict[str, Any] | None


@router.get('/audit', response_model=list[AuditEntryDTO])
async def list_audit_entries(
    request: Request,
    actor: Annotated[str | None, Query(description='Filter by actor.')] = None,
    action: Annotated[str | None, Query(description='Filter by action.')] = None,
    resource_type: Annotated[str | None, Query(description='Filter by resource type.')] = None,
    since: Annotated[
        datetime | None, Query(description='Only entries after this time (ISO 8601).')
    ] = None,
    until: Annotated[
        datetime | None, Query(description='Only entries before this time (ISO 8601).')
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500, description='Max entries to return.')] = 50,
    offset: Annotated[int, Query(ge=0, description='Pagination offset.')] = 0,
) -> list[AuditEntryDTO]:
    """Query the audit log with optional filters."""
    audit_service: AuditService = request.app.state.audit_service
    entries = await audit_service.query(
        actor=actor,
        action=action,
        resource_type=resource_type,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return [
        AuditEntryDTO(
            id=e.id,
            timestamp=e.timestamp,
            actor=e.actor,
            action=e.action,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            session_id=e.session_id,
            details=e.details,
        )
        for e in entries
    ]
