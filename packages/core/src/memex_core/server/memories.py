"""Memory unit endpoints."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from memex_common.exceptions import MemexError
from memex_core.server.auth import require_delete, require_read
from memex_common.schemas import MemoryLinkDTO, MemoryUnitDTO

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    build_memory_unit_dto,
    get_api,
)

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
