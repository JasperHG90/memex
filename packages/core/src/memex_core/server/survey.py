"""Survey endpoint — broad topic decomposition + parallel search."""

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends

from memex_common.exceptions import MemexError
from memex_common.schemas import SurveyRequest, SurveyResponse

from memex_core.api import MemexAPI
from memex_core.server.auth import AuthContext, check_vault_access, get_auth_context, require_read
from memex_core.server.common import _handle_error, get_api

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1', dependencies=[Depends(require_read)])


@router.post('/survey', response_model=SurveyResponse)
async def survey(
    request: Annotated[SurveyRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
):
    """Decompose a broad topic into sub-questions and return grouped results."""
    try:
        await check_vault_access(auth, request.vault_ids, api)
        result = await api.survey(
            query=request.query,
            vault_ids=request.vault_ids,
            limit_per_query=request.limit_per_query,
            token_budget=request.token_budget,
        )
        return result
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Survey failed')
