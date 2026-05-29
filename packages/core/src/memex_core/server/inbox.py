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
from sqlalchemy import text

from memex_core.api import MemexAPI
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server.inbox')

router = APIRouter(prefix='/api/v1/inbox', tags=['inbox'])


@router.get('/status', dependencies=[Depends(require_read)])
async def inbox_status(api: Annotated[MemexAPI, Depends(get_api)]) -> dict[str, Any]:
    """Router readiness + pending routing-proposal counts."""
    try:
        cfg = api.config.server.memory.inbox_router
        async with api.metastore.session() as session:
            match_n = (
                await session.execute(
                    text('SELECT n FROM inbox_router_nb_class_counts WHERE label = 1')
                )
            ).scalar()
            counts = (
                await session.execute(
                    text(
                        'SELECT rule_name, COUNT(*) FROM maintenance_proposals '
                        "WHERE lint_type = 'routing' AND status = 'pending' "
                        'GROUP BY rule_name'
                    )
                )
            ).all()
        pending = {r[0]: int(r[1]) for r in counts}
        match_count = float(match_n or 0.0)
        return {
            'enabled': cfg.enabled,
            'auto_apply_enabled': cfg.auto_apply_enabled,
            'warmed_up': match_count >= cfg.min_decisions_before_auto_apply,
            'match_observations': match_count,
            'min_decisions_before_auto_apply': cfg.min_decisions_before_auto_apply,
            'pending_route': pending.get('inbox_vault_route', 0),
            'pending_no_fit': pending.get('inbox_vault_no_fit', 0),
        }
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
