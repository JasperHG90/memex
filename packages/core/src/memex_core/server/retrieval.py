"""Retrieval endpoint."""

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends
from fastapi.responses import StreamingResponse

from memex_common.exceptions import MemexError
from memex_common.schemas import MemoryUnitDTO, RetrievalRequest

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    build_memory_unit_dto,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1')


@router.post(
    '/memories/search',
    response_class=StreamingResponse,
    responses=ndjson_openapi(MemoryUnitDTO, 'Stream of memory units with resolved lineage.'),
)
async def search_memories(
    request: Annotated[RetrievalRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background_tasks: BackgroundTasks,
):
    try:
        units, resonance_task = await api.search(
            query=request.query,
            limit=request.limit,
            vault_ids=request.vault_ids,
            token_budget=request.token_budget,
            strategies=request.strategies,
            include_stale=request.include_stale,
            include_superseded=request.include_superseded,
            debug=request.debug,
            after=request.after,
            before=request.before,
            tags=request.tags,
        )

        if resonance_task is not None:
            background_tasks.add_task(resonance_task)

        return ndjson_response([build_memory_unit_dto(u, debug=request.debug) for u in units])
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Retrieval failed')
