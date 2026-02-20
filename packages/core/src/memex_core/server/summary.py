"""Summary endpoint for AI-generated search result synthesis."""

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends

from memex_common.schemas import SummaryRequest, SummaryResponse
from memex_core.api import MemexAPI
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1')


@router.post(
    '/recall/summary',
    response_model=SummaryResponse,
    summary='Summarize search results',
    description='Generate an AI summary with citations from search result texts.',
)
async def summarize(
    request: Annotated[SummaryRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
) -> SummaryResponse:
    """Synthesize search results into a concise answer with citations."""
    try:
        summary = await api.summarize_search_results(
            query=request.query,
            texts=request.texts,
        )
        return SummaryResponse(summary=summary)
    except Exception as e:
        raise _handle_error(e, 'Summary generation failed')
