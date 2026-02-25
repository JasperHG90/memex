"""Entity endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from memex_common.schemas import EntityDTO, LineageResponse, MemoryUnitDTO

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    async_ndjson_response,
    build_document_dto,
    build_entity_dto,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

router = APIRouter(prefix='/api/v1')


@router.get(
    '/entities',
    response_class=StreamingResponse,
    responses=ndjson_openapi(EntityDTO, 'Stream of entities.'),
)
async def list_entities(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: int = 100,
    q: str | None = None,
    sort: Literal['-mentions'] | None = Query(
        None, description='Sort option: -mentions for top by mention count'
    ),
):
    """
    List entities.

    Query params:
    - limit: Maximum number of entities to return
    - q: Optional search query for name-based search
    - sort: Optional sort option. Use '-mentions' for top entities by mention count.
    """
    if q:
        try:
            entities = await api.search_entities(query=q, limit=limit)
            return ndjson_response([build_entity_dto(e) for e in entities])
        except Exception as e:
            raise _handle_error(e, 'Entity search failed')

    if sort == '-mentions':
        entities = await api.get_top_entities(limit=limit)
        return ndjson_response([build_entity_dto(e) for e in entities])

    async def ranked_stream():
        async for entity in api.list_entities_ranked(limit=limit):
            yield build_entity_dto(entity)

    return await async_ndjson_response(ranked_stream())


@router.get(
    '/cooccurrences',
    response_class=StreamingResponse,
    responses=ndjson_openapi(BaseModel, 'Stream of co-occurrence records.'),
)
async def get_bulk_cooccurrences(ids: str, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get co-occurrences for a set of entity IDs."""
    try:
        id_list = [UUID(i.strip()) for i in ids.split(',') if i.strip()]
        cos = await api.get_bulk_cooccurrences(id_list)
        items = [
            {
                'entity_id_1': c.entity_id_1,
                'entity_id_2': c.entity_id_2,
                'cooccurrence_count': c.cooccurrence_count,
                'vault_id': c.vault_id,
            }
            for c in cos
        ]
        return ndjson_response(items)
    except Exception as e:
        raise _handle_error(e, 'Failed to fetch bulk co-occurrences')


@router.get(
    '/entities/{id}/mentions',
    response_class=StreamingResponse,
    responses=ndjson_openapi(BaseModel, 'Stream of entity mentions.'),
)
async def get_entity_mentions(
    id: UUID, api: Annotated[MemexAPI, Depends(get_api)], limit: int = 20
):
    """Get mentions for an entity."""
    try:
        results = await api.get_entity_mentions(id, limit=limit)
        items = [
            {
                'unit': MemoryUnitDTO(
                    id=r['unit'].id,
                    text=r['unit'].text,
                    fact_type=r['unit'].fact_type,
                    metadata=r['unit'].unit_metadata,
                    document_id=r['unit'].document_id,
                    vault_id=r['unit'].vault_id,
                    mentioned_at=r['unit'].mentioned_at,
                    occurred_start=r['unit'].occurred_start,
                    occurred_end=r['unit'].occurred_end,
                ),
                'document': build_document_dto(r['document']),
            }
            for r in results
        ]
        return ndjson_response(items)
    except Exception as e:
        raise _handle_error(e, f'Failed to fetch mentions for entity {id}')


@router.get('/entities/{id}', response_model=EntityDTO)
async def get_entity(id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get entity details."""
    try:
        entity = await api.get_entity(id)
        if not entity:
            raise HTTPException(status_code=404, detail=f'Entity {id} not found')
        return build_entity_dto(entity)
    except Exception as e:
        raise _handle_error(e, f'Failed to get entity {id}')


@router.get(
    '/entities/{id}/cooccurrences',
    response_class=StreamingResponse,
    responses=ndjson_openapi(BaseModel, 'Stream of co-occurrence records for an entity.'),
)
async def get_entity_cooccurrences(id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get co-occurrence edges for an entity."""
    try:
        cos = await api.get_entity_cooccurrences(id)
        items = [
            {
                'entity_id_1': c.entity_id_1,
                'entity_id_2': c.entity_id_2,
                'cooccurrence_count': c.cooccurrence_count,
                'vault_id': c.vault_id,
            }
            for c in cos
        ]
        return ndjson_response(items)
    except Exception as e:
        raise _handle_error(e, f'Failed to fetch co-occurrences for entity {id}')


@router.get('/entities/{id}/lineage', response_model=LineageResponse)
async def get_entity_lineage(
    id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    direction: str = 'upstream',
    depth: int = 3,
    limit: int = 10,
):
    """Get the lineage of an entity."""
    try:
        from memex_common.schemas import LineageDirection

        return await api.get_lineage(
            entity_type='entity',
            entity_id=id,
            direction=LineageDirection(direction),
            depth=depth,
            limit=limit,
        )
    except Exception as e:
        raise _handle_error(e, f'Failed to retrieve lineage for entity {id}')


@router.delete('/entities/{entity_id}')
async def delete_entity(entity_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete an entity and all associated data (mental models, aliases, links, cooccurrences)."""
    try:
        await api.delete_entity(entity_id)
        return {'status': 'success'}
    except Exception as e:
        raise _handle_error(e, 'Entity deletion failed')


@router.delete('/entities/{entity_id}/mental-model')
async def delete_mental_model(
    entity_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: UUID | None = None,
):
    """Delete a mental model for a specific entity in a specific vault."""
    try:
        resolved_vault_id = await api.resolve_vault_identifier(
            vault_id or api.config.server.active_vault
        )
        await api.delete_mental_model(entity_id, resolved_vault_id)
        return {'status': 'success'}
    except Exception as e:
        raise _handle_error(e, 'Mental model deletion failed')
