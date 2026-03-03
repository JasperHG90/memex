"""Retrieval endpoint."""

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from fastapi.responses import StreamingResponse

from memex_common.exceptions import MemexError
from memex_common.schemas import MemoryUnitDTO, RetrievalRequest, StrategyDebugInfo

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1')


def _build_retrieval_dtos(
    units: list[Any],
    debug: bool = False,
) -> list[MemoryUnitDTO]:
    """Convert memory units to DTOs with resolved source document lineage."""
    dtos = []
    for u in units:
        doc_id = getattr(u, 'note_id', None)
        source_docs: list[UUID] = [doc_id] if doc_id else []

        # Build debug_info from engine-attached data
        debug_info: list[StrategyDebugInfo] | None = None
        if debug:
            raw_debug = getattr(u, '_debug_info', None)
            if raw_debug:
                debug_info = [
                    StrategyDebugInfo(
                        strategy_name=c.strategy_name,
                        rank=c.rank,
                        rrf_score=c.rrf_score,
                        raw_score=c.raw_score,
                        timing_ms=c.timing_ms,
                    )
                    for c in raw_debug
                ]

        dtos.append(
            MemoryUnitDTO(
                id=u.id,
                note_id=doc_id,
                source_note_ids=source_docs,
                text=u.text,
                fact_type=u.fact_type,
                status=u.status,
                mentioned_at=u.mentioned_at or u.event_date,
                occurred_start=u.occurred_start,
                occurred_end=u.occurred_end,
                vault_id=u.vault_id,
                metadata=u.unit_metadata,
                score=getattr(u, 'score', None),
                debug_info=debug_info,
            )
        )
    return dtos


@router.post(
    '/memories/search',
    response_class=StreamingResponse,
    responses=ndjson_openapi(MemoryUnitDTO, 'Stream of memory units with resolved lineage.'),
)
async def search_memories(
    request: Annotated[RetrievalRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    try:
        units = await api.search(
            query=request.query,
            limit=request.limit,
            vault_ids=request.vault_ids,
            token_budget=request.token_budget,
            strategies=request.strategies,
            include_stale=request.include_stale,
            debug=request.debug,
        )

        return ndjson_response(_build_retrieval_dtos(units, debug=request.debug))
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Retrieval failed')
