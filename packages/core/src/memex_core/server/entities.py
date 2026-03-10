"""Entity endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from memex_common.exceptions import MemexError
from memex_common.schemas import EntityDTO, LineageResponse, MemoryUnitDTO

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    async_ndjson_response,
    build_note_dto,
    build_entity_dto,
    get_api,
    ndjson_openapi,
    ndjson_response,
    resolve_vault_ids,
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
    vault_id: list[str] | None = Query(None, description='Filter by vault ID(s) or name(s)'),
):
    """
    List entities.

    Query params:
    - limit: Maximum number of entities to return
    - q: Optional search query for name-based search
    - sort: Optional sort option. Use '-mentions' for top entities by mention count.
    - vault_id: Optional vault ID(s) or name(s) to filter by. Repeat for multiple vaults.
    """
    vault_ids = await resolve_vault_ids(api, vault_id)
    if q:
        try:
            entities = await api.search_entities(query=q, limit=limit, vault_ids=vault_ids)
            return ndjson_response([build_entity_dto(e) for e in entities])
        except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
            raise _handle_error(e, 'Entity search failed')

    if sort == '-mentions':
        entities = await api.get_top_entities(limit=limit, vault_ids=vault_ids)
        return ndjson_response([build_entity_dto(e) for e in entities])

    async def ranked_stream():
        async for entity in api.list_entities_ranked(limit=limit, vault_ids=vault_ids):
            yield build_entity_dto(entity)

    return await async_ndjson_response(ranked_stream())


@router.get(
    '/cooccurrences',
    response_class=StreamingResponse,
    responses=ndjson_openapi(BaseModel, 'Stream of co-occurrence records.'),
)
async def get_bulk_cooccurrences(
    ids: str,
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: list[str] | None = Query(None, description='Filter by vault ID(s) or name(s)'),
):
    """Get co-occurrences for a set of entity IDs."""
    try:
        id_list = [UUID(i.strip()) for i in ids.split(',') if i.strip()]
        vault_ids = await resolve_vault_ids(api, vault_id)
        cos = await api.get_bulk_cooccurrences(id_list, vault_ids=vault_ids)
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
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to fetch bulk co-occurrences')


@router.get(
    '/entities/{id}/mentions',
    response_class=StreamingResponse,
    responses=ndjson_openapi(BaseModel, 'Stream of entity mentions.'),
)
async def get_entity_mentions(
    id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: int = 20,
    vault_id: list[str] | None = Query(None, description='Filter by vault ID(s) or name(s)'),
):
    """Get mentions for an entity."""
    try:
        vault_ids = await resolve_vault_ids(api, vault_id)
        results = await api.get_entity_mentions(id, limit=limit, vault_ids=vault_ids)
        items = [
            {
                'unit': MemoryUnitDTO(
                    id=r['unit'].id,
                    text=r['unit'].text,
                    fact_type=r['unit'].fact_type,
                    status=r['unit'].status,
                    metadata=r['unit'].unit_metadata,
                    note_id=r['unit'].note_id,
                    vault_id=r['unit'].vault_id,
                    mentioned_at=r['unit'].mentioned_at,
                    occurred_start=r['unit'].occurred_start,
                    occurred_end=r['unit'].occurred_end,
                    chunk_id=getattr(r['unit'], 'chunk_id', None),
                    confidence=getattr(r['unit'], 'confidence', 1.0) or 1.0,
                ),
                'note': build_note_dto(r['document']),
            }
            for r in results
        ]
        return ndjson_response(items)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to fetch mentions for entity {id}')


class BatchEntityRequest(BaseModel):
    entity_ids: list[UUID]


@router.post('/entities/batch', response_model=list[EntityDTO])
async def get_entities_batch(
    request: Annotated[BatchEntityRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Get multiple entities by ID."""
    try:
        entities = await api.get_entities(request.entity_ids)
        return [build_entity_dto(e) for e in entities]
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get entities batch')


@router.get('/entities/{id}', response_model=EntityDTO)
async def get_entity(id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get entity details."""
    try:
        entity = await api.get_entity(id)
        if not entity:
            raise HTTPException(status_code=404, detail=f'Entity {id} not found')
        return build_entity_dto(entity)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to get entity {id}')


@router.get(
    '/entities/{id}/cooccurrences',
    response_class=StreamingResponse,
    responses=ndjson_openapi(BaseModel, 'Stream of co-occurrence records for an entity.'),
)
async def get_entity_cooccurrences(
    id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: int = 50,
    vault_id: list[str] | None = Query(None, description='Filter by vault ID(s) or name(s)'),
):
    """Get co-occurrence edges for an entity."""
    try:
        vault_ids = await resolve_vault_ids(api, vault_id)
        cos = await api.get_entity_cooccurrences(id, vault_ids=vault_ids, limit=limit)
        items = [
            {
                'entity_id_1': c.entity_id_1,
                'entity_id_2': c.entity_id_2,
                'entity_1_name': c.entity_1.canonical_name,
                'entity_1_type': c.entity_1.entity_type,
                'entity_2_name': c.entity_2.canonical_name,
                'entity_2_type': c.entity_2.entity_type,
                'cooccurrence_count': c.cooccurrence_count,
                'vault_id': c.vault_id,
            }
            for c in cos
        ]
        return ndjson_response(items)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
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
            entity_type='mental_model',
            entity_id=id,
            direction=LineageDirection(direction),
            depth=depth,
            limit=limit,
        )
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to retrieve lineage for entity {id}')


@router.delete('/entities/{entity_id}')
async def delete_entity(entity_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete an entity and all associated data (mental models, aliases, links, cooccurrences)."""
    try:
        await api.delete_entity(entity_id)
        return {'status': 'success'}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Entity deletion failed')


@router.delete('/entities/{entity_id}/mental-model')
async def delete_mental_model(
    entity_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    vault_id: str | None = None,
):
    """Delete a mental model for a specific entity in a specific vault."""
    try:
        resolved_vault_id = await api.resolve_vault_identifier(
            vault_id or api.config.server.active_vault
        )
        await api.delete_mental_model(entity_id, resolved_vault_id)
        return {'status': 'success'}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Mental model deletion failed')
