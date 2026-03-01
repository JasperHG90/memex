"""Memory unit endpoints."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException

from memex_common.exceptions import MemexError
from memex_common.schemas import (
    AdjustBeliefRequest,
    AdjustBeliefResponse,
    EvidenceLogDTO,
    MemoryUnitDTO,
    SummaryRequest,
    SummaryResponse,
)

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    get_api,
)

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1')


@router.post(
    '/memories/summary',
    response_model=SummaryResponse,
    summary='Summarize search results',
    description='Generate an AI summary with citations from search result texts.',
)
async def summarize_memories(
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
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Summary generation failed')


@router.patch('/memories/{unit_uuid}/belief')
async def adjust_memory_belief(
    unit_uuid: UUID,
    request: Annotated[AdjustBeliefRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
) -> AdjustBeliefResponse:
    """Adjust belief confidence for a memory unit."""
    try:
        result = await api.adjust_belief(
            unit_uuid=unit_uuid,
            evidence_type_key=request.evidence_type_key,
            description=request.description,
        )
        return AdjustBeliefResponse(
            unit_id=unit_uuid,
            evidence_type=request.evidence_type_key,
            confidence_before=result['confidence_before'],
            confidence_after=result['confidence_after'],
            alpha=result['alpha'],
            beta=result['beta'],
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Belief adjustment failed')


@router.get('/memories/{unit_id}/evidence-log')
async def get_evidence_log(
    unit_id: UUID,
    limit: int = 20,
    api: MemexAPI = Depends(get_api),
) -> list[EvidenceLogDTO]:
    """Retrieve the evidence audit trail for a memory unit."""
    try:
        logs = await api.get_evidence_log(unit_id, limit=limit)
        return [EvidenceLogDTO(**log) for log in logs]
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to retrieve evidence log')


@router.get('/memories/{id}', response_model=MemoryUnitDTO)
async def get_memory_unit(id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get memory unit details."""
    try:
        unit = await api.get_memory_unit(id)
        if not unit:
            raise HTTPException(status_code=404, detail=f'Memory unit {id} not found')

        return MemoryUnitDTO(
            id=unit.id,
            text=unit.text,
            fact_type=unit.fact_type,
            metadata=unit.unit_metadata,
            note_id=unit.note_id,
            vault_id=unit.vault_id,
            mentioned_at=unit.mentioned_at,
            occurred_start=unit.occurred_start,
            occurred_end=unit.occurred_end,
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to get memory unit {id}')


@router.delete('/memories/{id}')
async def delete_memory_unit(id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete a memory unit and all associated data (entity links, memory links, evidence)."""
    try:
        await api.delete_memory_unit(id)
        return {'status': 'success'}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Memory unit deletion failed')
