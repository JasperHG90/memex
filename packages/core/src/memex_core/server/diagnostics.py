"""Diagnostics endpoints (manifold + retrieval heatmap + summary + lint).

Routes:
- GET /diagnostics/manifold/{vault_id}              — 200 (warm) | 202 (cold) | 501 (umap missing)
- GET /diagnostics/manifold/{vault_id}/status       — 200 (done) | 202 (still computing) | 404
- GET /diagnostics/retrieval/{vault_id}             — 200 with heatmap JSON
- GET /diagnostics/summary/{vault_id}               — 200 with full summary
- GET /diagnostics/lint/{vault_id}                  — 200 with lint pivot dashboard
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from memex_core.api import MemexAPI
from memex_core.diagnostics import compute_heatmap
from memex_core.diagnostics.umap import UMAPNotInstalledError
from memex_core.server.auth import (
    AuthContext,
    check_vault_access,
    get_auth_context,
    require_read,
)
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server.diagnostics')

router = APIRouter(prefix='/api/v1/diagnostics', dependencies=[Depends(require_read)])

_UMAP_MISSING_DETAIL = 'umap-learn not installed; install memex[diagnostics]'


def _ensure_umap_available() -> None:
    try:
        import umap  # noqa: F401
    except ImportError as e:
        raise HTTPException(status_code=501, detail=_UMAP_MISSING_DETAIL) from e


@router.get('/manifold/{vault_id}')
async def get_manifold(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    force_refresh: bool = Query(False, alias='force_refresh'),
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> JSONResponse:
    """Returns the cached UMAP projection if warm, or 202 with a task_id on cold.

    Returns 501 if `umap-learn` is not installed.
    """
    _ensure_umap_available()
    await check_vault_access(auth, [vault_id], api)
    try:
        status, payload = await api.diagnostics.get_or_compute_manifold(
            vault_id, force_refresh=force_refresh
        )
    except UMAPNotInstalledError as e:
        raise HTTPException(status_code=501, detail=_UMAP_MISSING_DETAIL) from e
    except Exception as e:
        raise _handle_error(e, 'Failed to get manifold')

    if status == 'ready':
        return JSONResponse(content=payload, status_code=200)
    if status == 'computing':
        return JSONResponse(content=payload, status_code=202)
    raise HTTPException(status_code=500, detail=f'Unexpected manifold status: {status}')


@router.get('/manifold/{vault_id}/status')
async def get_manifold_status(
    vault_id: UUID,
    task_id: Annotated[str, Query(...)],
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> JSONResponse:
    """Polling endpoint for an in-flight manifold compute."""
    await check_vault_access(auth, [vault_id], api)
    try:
        status, payload = await api.diagnostics.get_manifold_status(vault_id, task_id)
    except Exception as e:
        raise _handle_error(e, 'Failed to query manifold status')

    if status == 'ready':
        return JSONResponse(content=payload, status_code=200)
    if status == 'computing':
        return JSONResponse(content=payload, status_code=202)
    if status == 'unavailable':
        raise HTTPException(status_code=501, detail=_UMAP_MISSING_DETAIL)
    if status == 'absent':
        raise HTTPException(
            status_code=404, detail='No manifold task or cache for this vault/task_id.'
        )
    raise HTTPException(status_code=500, detail=f'Unexpected manifold status: {status}')


@router.get('/retrieval/{vault_id}')
async def get_retrieval_heatmap(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    top_n: int = Query(50, ge=1, le=500),
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Top-N entities by outcome volume — independent of UMAP cache."""
    await check_vault_access(auth, [vault_id], api)
    try:
        return await compute_heatmap(api.metastore, vault_id, top_n=top_n)
    except Exception as e:
        raise _handle_error(e, 'Failed to compute retrieval heatmap')


@router.get('/summary/{vault_id}')
async def get_diagnostics_summary(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """High-level diagnostics summary (synchronous, no UMAP block)."""
    await check_vault_access(auth, [vault_id], api)
    try:
        return await api.diagnostics.get_summary(vault_id)
    except Exception as e:
        raise _handle_error(e, 'Failed to compute diagnostics summary')


@router.get('/lint/{vault_id}')
async def get_lint_dashboard(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
) -> dict[str, Any]:
    """Lint dashboard pivot for one vault.

    Returns the (lint_type, status, source) pivot, the pending_by_type
    slice, and the top-5 most-recent pending findings. Distinct from the
    /lint/status (single count) and /lint/findings (paginated row listing) —
    this is the operator/observability dashboard view.
    """
    await check_vault_access(auth, [vault_id], api)
    try:
        return await api.diagnostics.get_lint_dashboard(vault_id)
    except Exception as e:
        raise _handle_error(e, 'Failed to compute lint dashboard')
