"""Reflection endpoints."""

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query
from fastapi.responses import StreamingResponse

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.schemas import (
    ReflectionQueueDTO,
    ReflectionRequest as ReflectionDTO,
    ReflectionResultDTO,
)

from memex_core.api import MemexAPI
from memex_core.memory.reflect.models import ReflectionRequest as CoreReflectionRequest
from memex_core.server.common import (
    _handle_error,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

router = APIRouter(prefix='/api/v1')


@router.post('/reflections', response_model=ReflectionResultDTO)
async def reflect(
    request: Annotated[ReflectionDTO, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background_tasks: BackgroundTasks,
):
    """Trigger reflection on an entity."""
    try:
        internal_req = CoreReflectionRequest(
            entity_id=request.entity_id,
            limit_recent_memories=request.limit_recent_memories,
            vault_id=request.vault_id or GLOBAL_VAULT_ID,
        )

        background_tasks.add_task(api.background_reflect, internal_req)

        return ReflectionResultDTO(
            entity_id=request.entity_id,
            new_observations=[],
            status='queued',
        )
    except Exception as e:
        raise _handle_error(e, 'Reflection failed')


@router.post(
    '/reflections/batch',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionResultDTO, 'Stream of reflection results.'),
)
async def reflect_batch(
    request: Annotated[dict[str, list[ReflectionDTO]], Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background_tasks: BackgroundTasks,
):
    """Trigger reflection on a batch of entities."""
    try:
        internal_reqs = [
            CoreReflectionRequest(
                entity_id=r.entity_id,
                limit_recent_memories=r.limit_recent_memories,
                vault_id=r.vault_id or GLOBAL_VAULT_ID,
            )
            for r in request['requests']
        ]

        background_tasks.add_task(api.background_reflect_batch, internal_reqs)

        return ndjson_response(
            [
                ReflectionResultDTO(
                    entity_id=req.entity_id,
                    new_observations=[],
                    status='queued',
                )
                for req in internal_reqs
            ]
        )
    except Exception as e:
        raise _handle_error(e, 'Batch reflection failed')


@router.get(
    '/reflections',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionQueueDTO, 'Stream of reflection queue items.'),
)
async def list_reflections(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: int = 10,
    status: Literal['queued'] | None = Query(None, description='Filter by status'),
):
    """
    List reflections.

    Query params:
    - limit: Maximum number of items to return
    - status: Optional filter by status. Use 'queued' for queue items.
    """
    try:
        if status == 'queued':
            items = await api.get_reflection_queue_batch(limit=limit)
            return ndjson_response(
                [
                    ReflectionQueueDTO(
                        entity_id=item.entity_id,
                        vault_id=item.vault_id,
                        priority_score=item.priority_score,
                    )
                    for item in items
                ]
            )
        # Add other status filters if needed in the future
        return ndjson_response([])
    except Exception as e:
        raise _handle_error(e, 'Failed to list reflections')


@router.post(
    '/reflections/claim',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionQueueDTO, 'Stream of claimed reflection queue items.'),
)
async def claim_reflections(api: Annotated[MemexAPI, Depends(get_api)], limit: int = 10):
    """Claim reflection queue items for processing."""
    try:
        items = await api.claim_reflection_queue_batch(limit=limit)
        return ndjson_response(
            [
                ReflectionQueueDTO(
                    entity_id=item.entity_id,
                    vault_id=item.vault_id,
                    priority_score=item.priority_score,
                )
                for item in items
            ]
        )
    except Exception as e:
        raise _handle_error(e, 'Failed to claim reflection tasks')
