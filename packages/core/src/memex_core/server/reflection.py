"""Reflection endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.server.auth import require_delete, require_write
from memex_common.exceptions import MemexError
from memex_common.schemas import (
    DeadLetterItemDTO,
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
    resolve_vault_ids,
)

router = APIRouter(prefix='/api/v1')


@router.post(
    '/reflections', response_model=ReflectionResultDTO, dependencies=[Depends(require_write)]
)
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
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Reflection failed')


@router.post(
    '/reflections/batch',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionResultDTO, 'Stream of reflection results.'),
    dependencies=[Depends(require_write)],
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
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Batch reflection failed')


@router.get(
    '/reflections',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionQueueDTO, 'Stream of reflection queue items.'),
    dependencies=[Depends(require_write)],
)
async def list_reflections(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: Annotated[int, Query(ge=1, le=500)] = 10,
    status: Literal['queued'] | None = Query(None, description='Filter by status'),
    vault_id: list[str] | None = Query(None, description='Filter by vault ID(s) or name(s)'),
):
    """
    List reflections.

    Query params:
    - limit: Maximum number of items to return
    - status: Optional filter by status. Use 'queued' for queue items.
    - vault_id: Optional vault ID(s) or name(s) to filter by.
    """
    try:
        resolved = await resolve_vault_ids(api, vault_id)
        if status == 'queued':
            items = await api.get_reflection_queue_batch(limit=limit, vault_ids=resolved)
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
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to list reflections')


@router.post(
    '/reflections/claim',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionQueueDTO, 'Stream of claimed reflection queue items.'),
    dependencies=[Depends(require_write)],
)
async def claim_reflections(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: Annotated[int, Query(ge=1, le=500)] = 10,
):
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
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to claim reflection tasks')


# ---------------------------------------------------------------------------
# Dead Letter Queue (DLQ) admin endpoints
# ---------------------------------------------------------------------------


@router.get(
    '/admin/reflection/dlq',
    response_model=list[DeadLetterItemDTO],
    dependencies=[Depends(require_delete)],
)
async def list_dead_letter_items(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    vault_id: Annotated[str | None, Query(description='Filter by vault ID or name.')] = None,
) -> list[DeadLetterItemDTO]:
    """List dead-lettered reflection tasks that exhausted their retries."""
    try:
        resolved_vault_id = await api.resolve_vault_identifier(vault_id) if vault_id else None
        items = await api.get_dead_letter_items(
            limit=limit,
            offset=offset,
            vault_id=resolved_vault_id,
        )
        return [
            DeadLetterItemDTO(
                id=item.id,
                entity_id=item.entity_id,
                vault_id=item.vault_id,
                priority_score=item.priority_score,
                retry_count=item.retry_count,
                max_retries=item.max_retries,
                last_error=item.last_error,
                status=item.status.value if hasattr(item.status, 'value') else item.status,
            )
            for item in items
        ]
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to list dead letter items')


@router.post(
    '/admin/reflection/dlq/{item_id}/retry',
    response_model=DeadLetterItemDTO,
    dependencies=[Depends(require_delete)],
)
async def retry_dead_letter_item(
    item_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
) -> DeadLetterItemDTO:
    """Reset a dead-lettered item back to pending for re-processing."""
    try:
        item = await api.retry_dead_letter_item(item_id)
        if item is None:
            raise HTTPException(
                status_code=404,
                detail=f'Dead letter item {item_id} not found or not in dead_letter status.',
            )
        return DeadLetterItemDTO(
            id=item.id,
            entity_id=item.entity_id,
            vault_id=item.vault_id,
            priority_score=item.priority_score,
            retry_count=item.retry_count,
            max_retries=item.max_retries,
            last_error=item.last_error,
            status=item.status.value if hasattr(item.status, 'value') else item.status,
        )
    except HTTPException:
        raise
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to retry dead letter item')
