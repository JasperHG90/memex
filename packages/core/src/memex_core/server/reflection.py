"""Reflection and belief adjustment endpoints."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends
from fastapi.responses import StreamingResponse

from memex_common.config import GLOBAL_VAULT_ID
from memex_common.schemas import (
    AdjustBeliefRequest,
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


@router.post('/reflect', response_model=ReflectionResultDTO)
async def reflect(
    request: Annotated[ReflectionDTO, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background_tasks: BackgroundTasks,
):
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
    '/reflect/batch',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionResultDTO, 'Stream of reflection results.'),
)
async def reflect_batch(
    request: Annotated[dict[str, list[ReflectionDTO]], Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background_tasks: BackgroundTasks,
):
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
    '/reflect/queue',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionQueueDTO, 'Stream of reflection queue items.'),
)
async def get_reflection_queue(api: Annotated[MemexAPI, Depends(get_api)], limit: int = 10):
    try:
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
    except Exception as e:
        raise _handle_error(e, 'Failed to fetch reflection queue')


@router.post(
    '/reflect/queue/claim',
    response_class=StreamingResponse,
    responses=ndjson_openapi(ReflectionQueueDTO, 'Stream of claimed reflection queue items.'),
)
async def claim_reflection_queue(api: Annotated[MemexAPI, Depends(get_api)], limit: int = 10):
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


@router.post('/belief/adjust')
async def adjust_belief(
    request: Annotated[AdjustBeliefRequest, Body()], api: Annotated[MemexAPI, Depends(get_api)]
):
    try:
        await api.adjust_belief(
            unit_uuid=request.unit_uuid,
            evidence_type_key=request.evidence_type_key,
            description=request.description,
        )
        return {'status': 'success'}
    except Exception as e:
        raise _handle_error(e, 'Belief adjustment failed')
