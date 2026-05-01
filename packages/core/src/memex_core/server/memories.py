"""Memory unit endpoints."""

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from memex_common.exceptions import MemexError, MemoryUnitNotFoundError
from memex_core.server.auth import require_delete, require_read, require_write
from memex_common.schemas import MemoryLinkDTO, MemoryUnitDTO

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    build_memory_unit_dto,
    get_api,
)
from memex_core.services.locks import EntityLockTimeoutError
from memex_core.services.rate_limit import RateLimitExceededError

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1')


@router.get('/memories/{id}', response_model=MemoryUnitDTO, dependencies=[Depends(require_read)])
async def get_memory_unit(id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get memory unit details."""
    try:
        unit = await api.get_memory_unit(id)
        if not unit:
            raise HTTPException(status_code=404, detail=f'Memory unit {id} not found')

        return build_memory_unit_dto(unit)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to get memory unit {id}')


@router.delete('/memories/{id}', dependencies=[Depends(require_delete)])
async def delete_memory_unit(id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete a memory unit and all associated data (entity links, memory links, evidence)."""
    try:
        await api.delete_memory_unit(id)
        return {'status': 'success'}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Memory unit deletion failed')


class DeprioritizeRequest(BaseModel):
    reason: str = Field(..., description='Why this unit was deprioritized (logged to audit_logs).')


@router.post(
    '/memories/{id}/deprioritize',
    response_model=MemoryUnitDTO,
    dependencies=[Depends(require_write)],
)
async def deprioritize_memory_unit(
    id: UUID,
    request: DeprioritizeRequest,
    background_tasks: BackgroundTasks,
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Deprioritize a memory unit (non-destructive — flips ``is_deprioritized=True``).

    Per Wave 0 §6 #12: this is the NON-destructive curation verb. Archive
    remains the destructive counterpart.
    """
    try:
        unit = await api.deprioritize_memory_unit(
            id, reason=request.reason, background_tasks=background_tasks
        )
        return build_memory_unit_dto(unit)
    except MemoryUnitNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to deprioritize memory unit {id}')


@router.post(
    '/memories/{id}/restore',
    response_model=MemoryUnitDTO,
    dependencies=[Depends(require_write)],
)
async def restore_memory_unit(
    id: UUID,
    background_tasks: BackgroundTasks,
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Restore a deprioritized memory unit (flips ``is_deprioritized=False``)."""
    try:
        unit = await api.restore_memory_unit(id, background_tasks=background_tasks)
        return build_memory_unit_dto(unit)
    except MemoryUnitNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to restore memory unit {id}')


# ---------------------------------------------------------------------------
# F9 — memex_memory_reconsolidate / memex_memory_consolidate
# ---------------------------------------------------------------------------


class ReconsolidateRequest(BaseModel):
    """F9: per-entity reconsolidation under an advisory lock."""

    entity_id: UUID = Field(..., description='Entity UUID to reconsolidate.')
    vault_id: UUID = Field(..., description='Vault UUID — explicit per RFC-005 to scope unit_ids.')
    timeout_seconds: float = Field(
        default=30.0,
        ge=0.1,
        le=300.0,
        description='Advisory lock acquisition timeout.',
    )


class ConsolidateRequest(BaseModel):
    """F9: vault-wide low-MW consolidation."""

    vault_id: UUID = Field(..., description='Vault UUID to consolidate.')
    dry_run: bool = Field(
        default=False,
        description='If true, return preview without writes.',
    )


@router.post(
    '/memory/reconsolidate',
    dependencies=[Depends(require_write)],
    responses={
        409: {
            'description': (
                'Concurrent reconsolidation in progress on this entity (advisory lock contention).'
            )
        }
    },
)
async def reconsolidate_entity(
    request: Annotated[ReconsolidateRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
) -> dict[str, Any]:
    """F9: re-evaluate memories for an entity under a per-entity advisory lock.

    Acquires `acquire_entity_lock(entity_id)` for `timeout_seconds`, then runs
    `ContradictionEngine.detect_contradictions` over linked unit_ids, then
    `ReflectionService.reflect_batch` for the entity. RFC-005 / RFC-008.
    """
    try:
        return await api.reconsolidate_entity(
            request.entity_id,
            request.vault_id,
            timeout_seconds=request.timeout_seconds,
        )
    except EntityLockTimeoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Reconsolidate failed')


@router.post(
    '/memory/consolidate',
    dependencies=[Depends(require_write)],
    responses={
        429: {
            'description': 'Rate limit exceeded for this vault (default 1 call per vault per hour).',
            'content': {
                'application/json': {
                    'example': {
                        'error': 'rate_limit_exceeded',
                        'retry_after_seconds': 1234.5,
                        'message': 'Rate limit exceeded; retry after 1234.50s.',
                    }
                }
            },
        }
    },
)
async def consolidate_vault(
    request: Annotated[ConsolidateRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
) -> Any:
    """F9: vault-wide low-MW unit consolidation. RFC-008.

    Predicate: mw_score<0.35 AND outcomes>=5 AND !is_deprioritized AND
    created_at<now()-30d. dry_run=true returns preview without writes.

    Rate-limited per vault (RFC-008 line 125; default 1 call per vault per
    hour). Translates ``RateLimitExceededError`` into a 429 envelope with a
    ``Retry-After`` header. Mirrors the F5 summarize-node 429 contract.
    """
    try:
        return await api.consolidate_vault(request.vault_id, dry_run=request.dry_run)
    except RateLimitExceededError as exc:
        retry_after = max(0, int(exc.retry_after_seconds + 0.999))
        return JSONResponse(
            status_code=429,
            content={
                'error': 'rate_limit_exceeded',
                'retry_after_seconds': exc.retry_after_seconds,
                'message': str(exc),
            },
            headers={'Retry-After': str(retry_after)},
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Consolidate failed')


@router.get(
    '/memories/{memory_id}/links',
    response_model=list[MemoryLinkDTO],
    dependencies=[Depends(require_read)],
)
async def get_memory_links(
    memory_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    link_type: str | None = Query(None, description='Filter by link type (e.g. contradicts).'),
    limit: int = Query(20, ge=1, le=200, description='Max links to return.'),
) -> list[MemoryLinkDTO]:
    """Get typed relationship links for a memory unit."""
    try:
        link_types = [link_type] if link_type else None
        links_map = await api.get_memory_links([memory_id], link_types=link_types)
        links = links_map.get(memory_id, [])
        return links[:limit]
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to get links for memory unit {memory_id}')
