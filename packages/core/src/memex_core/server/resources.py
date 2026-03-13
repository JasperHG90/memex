"""Resource and lineage endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from memex_common.exceptions import MemexError
from memex_common.schemas import LineageDirection, LineageResponse

from memex_core.api import MemexAPI
from memex_core.server.common import _handle_error, get_api

router = APIRouter(prefix='/api/v1')


@router.get('/resources/{path:path}')
async def get_resource(path: str, api: Annotated[MemexAPI, Depends(get_api)]):
    """Retrieve a raw resource (file) from the filestore."""
    import mimetypes

    from fastapi.responses import Response

    try:
        content = await api.get_resource(path)
        media_type, _ = mimetypes.guess_type(path)
        return Response(content=content, media_type=media_type or 'application/octet-stream')
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f'Resource not found: {path}')
    except (MemexError, ValueError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to retrieve resource')


# Deprecated: use GET /lineage/note/{id} instead. Remove after 2026-06-01.
@router.get('/notes/{id}/lineage', response_model=LineageResponse)
async def get_note_lineage(
    id: UUID,
    response: Response,
    api: Annotated[MemexAPI, Depends(get_api)],
    direction: LineageDirection = LineageDirection.UPSTREAM,
    depth: Annotated[int, Query(ge=1, le=10)] = 3,
    limit: Annotated[int, Query(ge=1, le=500)] = 10,
):
    """Get the lineage of a note.

    .. deprecated:: Use ``GET /api/v1/lineage/note/{id}`` instead.
    """
    response.headers['Deprecation'] = 'true'
    response.headers['Sunset'] = '2026-06-01'
    response.headers['Link'] = '</api/v1/lineage>; rel="successor-version"'
    try:
        return await api.get_lineage(
            entity_type='note',
            entity_id=id,
            direction=direction,
            depth=depth,
            limit=limit,
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to retrieve lineage for note {id}')


VALID_LINEAGE_TYPES = {'note', 'entity', 'memory_unit', 'observation', 'mental_model'}


@router.get('/lineage/{entity_type}/{id}', response_model=LineageResponse)
async def get_lineage(
    entity_type: str,
    id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    direction: LineageDirection = LineageDirection.UPSTREAM,
    depth: Annotated[int, Query(ge=1, le=10)] = 3,
    limit: Annotated[int, Query(ge=1, le=500)] = 10,
):
    """Get the lineage of any entity type."""
    if entity_type not in VALID_LINEAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f'Invalid entity type: {entity_type}. Must be one of: {VALID_LINEAGE_TYPES}',
        )
    # The API only understands 'mental_model', not 'entity'
    resolved_type = 'mental_model' if entity_type == 'entity' else entity_type
    try:
        return await api.get_lineage(
            entity_type=resolved_type,
            entity_id=id,
            direction=direction,
            depth=depth,
            limit=limit,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to retrieve lineage for {entity_type} {id}')
