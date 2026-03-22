"""Retrieval endpoint."""

import logging
import time
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends
from fastapi.responses import StreamingResponse

from memex_common.exceptions import MemexError
from memex_common.schemas import MemoryUnitDTO, RetrievalRequest

from memex_core.api import MemexAPI
from memex_core.server.auth import AuthContext, check_vault_access, get_auth_context, require_read
from memex_core.server.common import (
    _handle_error,
    build_memory_unit_dto,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1', dependencies=[Depends(require_read)])


@router.post(
    '/memories/search',
    response_class=StreamingResponse,
    responses=ndjson_openapi(MemoryUnitDTO, 'Stream of memory units with resolved lineage.'),
)
async def search_memories(
    request: Annotated[RetrievalRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background_tasks: BackgroundTasks,
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
):
    try:
        await check_vault_access(auth, request.vault_ids, api)
        t0 = time.monotonic()
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
        t_search = time.monotonic() - t0

        if resonance_task is not None:
            background_tasks.add_task(resonance_task)

        t0 = time.monotonic()
        dtos = [build_memory_unit_dto(u, debug=request.debug) for u in units]
        t_serialize = time.monotonic() - t0

        logger.warning(
            'PROFILE endpoint | search=%.0fms serialize=%.0fms total=%.0fms | results=%d',
            t_search * 1000,
            t_serialize * 1000,
            (t_search + t_serialize) * 1000,
            len(units),
        )

        return ndjson_response(dtos)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Retrieval failed')
