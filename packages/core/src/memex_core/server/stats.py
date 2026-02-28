"""Stats endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from memex_common.schemas import SystemStatsCountsDTO, TokenUsageResponse

from memex_core.api import MemexAPI
from memex_core.server.common import _handle_error, get_api

router = APIRouter(prefix='/api/v1')


@router.get('/stats/counts', response_model=SystemStatsCountsDTO)
async def get_stats_counts(
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: UUID | None = Query(None, description='Filter by vault ID'),
):
    """Get total counts for documents, entities, and reflection queue."""
    try:
        counts = await api.get_stats_counts(vault_id=vault_id)
        return SystemStatsCountsDTO(**counts)
    except Exception as e:
        raise _handle_error(e, 'Failed to fetch system stats')


@router.get('/stats/token-usage', response_model=TokenUsageResponse)
async def get_token_usage(api: Annotated[MemexAPI, Depends(get_api)]):
    """Get daily aggregated token usage."""
    try:
        usage = await api.get_daily_token_usage()
        return TokenUsageResponse(usage=usage)
    except Exception as e:
        raise _handle_error(e, 'Failed to fetch token usage stats')
