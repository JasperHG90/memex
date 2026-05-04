"""Consolidation orchestrator endpoints.

Operator-facing routes for the `memex consolidate` CLI. Per acceptance criteria
there is intentionally NO MCP / Hermes / Claude Code tool surface — these are
HTTP-only and the CLI is the only first-class client.

Routes:
- POST /api/v1/consolidation/tick      — run a tick for one (or all) vaults
- GET  /api/v1/consolidation/status    — last-run timestamps per vault
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from memex_core.api import MemexAPI
from memex_core.server.auth import require_read, require_write
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server.consolidation')

router = APIRouter(prefix='/api/v1/consolidation')


class ConsolidationTickRequest(BaseModel):
    vault_id: UUID | None = Field(
        default=None,
        description='Single vault to tick. None = tick every vault sequentially.',
    )
    dry_run: bool = Field(
        default=False,
        description='Skip writes; return per-step counts only.',
    )
    budget: int | None = Field(
        default=None,
        ge=1,
        le=10_000,
        description='Override units-per-tick budget. None = config default.',
    )


@router.post('/tick', dependencies=[Depends(require_write)])
async def post_tick(
    body: ConsolidationTickRequest,
    api: Annotated[MemexAPI, Depends(get_api)],
) -> dict[str, Any]:
    """Run consolidation tick(s) immediately. Returns per-vault summaries."""
    try:
        budget = body.budget or api.config.server.memory.consolidation.units_per_tick
        if body.vault_id is not None:
            result = await api.consolidation.tick(
                body.vault_id, dry_run=body.dry_run, budget=budget
            )
            return {'ticks': [result]}

        vaults = await api.list_vaults()
        results: list[dict[str, Any]] = []
        for vault in vaults:
            try:
                results.append(
                    await api.consolidation.tick(vault.id, dry_run=body.dry_run, budget=budget)
                )
            except Exception as exc:
                logger.warning('Consolidation tick failed for vault %s: %s', vault.id, exc)
                results.append({'vault_id': str(vault.id), 'error': str(exc)})
        return {'ticks': results}
    except Exception as exc:
        raise _handle_error(exc, 'Failed to run consolidation tick')


@router.get('/status', dependencies=[Depends(require_read)])
async def get_status(
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: UUID | None = Query(default=None),
) -> dict[str, Any]:
    """Return the most recent tick row per vault (or for one vault)."""
    try:
        rows = await api.consolidation.status(vault_id)
        return {'ticks': rows}
    except Exception as exc:
        raise _handle_error(exc, 'Failed to query consolidation status')
