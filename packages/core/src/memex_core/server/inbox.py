"""Inbox router endpoints.

Routes:
- GET  /api/v1/inbox/status   — router readiness + pending routing-proposal counts
- POST /api/v1/inbox/triage   — run one triage tick (``dry_run`` to preview only)

The triage endpoint backs ``memex inbox triage`` (CLI) and the eval suite's
trigger. With ``dry_run=false`` it can migrate notes (auto-route) and emit
proposals, so it is a write surface gated by ``require_write``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from memex_core.api import MemexAPI
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server.inbox')

router = APIRouter(prefix='/api/v1/inbox', tags=['inbox'])


@router.get('/status', dependencies=[Depends(require_read)])
async def inbox_status(api: Annotated[MemexAPI, Depends(get_api)]) -> dict[str, Any]:
    """Router readiness + pending routing-proposal counts."""
    try:
        return await api.inbox_router.status()
    except Exception as exc:
        raise _handle_error(exc, 'Failed to read inbox-router status')


@router.post('/triage', dependencies=[Depends(require_write)])
async def inbox_triage(
    api: Annotated[MemexAPI, Depends(get_api)],
    dry_run: Annotated[bool, Query(description='Score + decide without mutating.')] = False,
) -> dict[str, Any]:
    """Run one inbox triage tick. Returns the per-outcome counts."""
    try:
        await api.inbox_router.ensure_inbox_vault()
        result = await api.inbox_router.triage_tick(dry_run=dry_run)
        return {'dry_run': dry_run, **result.as_dict()}
    except Exception as exc:
        raise _handle_error(exc, 'Failed to run inbox triage')
