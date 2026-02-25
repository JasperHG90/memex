"""Resource and lineage endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

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
    except Exception as e:
        if isinstance(e, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f'Resource not found: {path}')
        raise _handle_error(e, 'Failed to retrieve resource')


@router.get('/notes/{id}/lineage', response_model=LineageResponse)
async def get_note_lineage(
    id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    direction: LineageDirection = LineageDirection.UPSTREAM,
    depth: int = 3,
    limit: int = 10,
):
    """Get the lineage of a note."""
    try:
        return await api.get_lineage(
            entity_type='note',
            entity_id=id,
            direction=direction,
            depth=depth,
            limit=limit,
        )
    except Exception as e:
        raise _handle_error(e, f'Failed to retrieve lineage for note {id}')
