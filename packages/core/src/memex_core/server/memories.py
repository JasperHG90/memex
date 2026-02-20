"""Memory unit endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from memex_common.schemas import MemoryUnitDTO

from memex_core.api import MemexAPI
from memex_core.server.common import _handle_error, get_api

router = APIRouter(prefix='/api/v1')


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
            document_id=unit.document_id,
            vault_id=unit.vault_id,
            mentioned_at=unit.mentioned_at,
            occurred_start=unit.occurred_start,
            occurred_end=unit.occurred_end,
        )
    except Exception as e:
        raise _handle_error(e, f'Failed to get memory unit {id}')


@router.delete('/memories/{id}')
async def delete_memory_unit(id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete a memory unit and all associated data (entity links, memory links, evidence)."""
    try:
        await api.delete_memory_unit(id)
        return {'status': 'success'}
    except Exception as e:
        raise _handle_error(e, 'Memory unit deletion failed')
