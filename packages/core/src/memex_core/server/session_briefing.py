"""Session briefing endpoint."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from memex_core.api import MemexAPI
from memex_core.server.auth import require_read
from memex_core.server.common import _handle_error, get_api
from memex_common.exceptions import MemexError

_VALID_BUDGETS = (1000, 2000)

logger = logging.getLogger('memex.core.server.session_briefing')

router = APIRouter(prefix='/api/v1')


@router.get(
    '/vaults/{vault_id}/session-briefing',
    response_model=dict,
    dependencies=[Depends(require_read)],
    summary='Generate session briefing',
    description='Generate a token-budgeted session briefing for LLM agents.',
)
async def get_session_briefing(
    vault_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    budget: int = Query(2000, description='Token budget (1000 or 2000).'),
    project_id: str | None = Query(None, description='Optional project ID for KV scoping.'),
) -> dict:
    """Generate a session briefing for the given vault."""
    if budget not in _VALID_BUDGETS:
        raise HTTPException(
            status_code=422,
            detail=f'budget must be one of {_VALID_BUDGETS}, got {budget}',
        )
    try:
        briefing = await api.session_briefing.generate(
            vault_id=vault_id,
            budget=budget,
            project_id=project_id,
        )
        return {'briefing': briefing, 'vault_id': str(vault_id), 'budget': budget}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to generate session briefing')
