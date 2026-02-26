"""Note endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from memex_common.schemas import NoteDTO, NoteSearchRequest, NoteSearchResult, NodeDTO

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    build_document_dto,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

router = APIRouter(prefix='/api/v1')


@router.get(
    '/notes',
    response_class=StreamingResponse,
    responses=ndjson_openapi(NoteDTO, 'Stream of notes.'),
)
async def list_notes(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: int = 100,
    offset: int = 0,
    sort: Literal['-created_at'] | None = Query(
        None, description='Sort option: -created_at for recency'
    ),
):
    """
    List notes.

    Query params:
    - limit: Maximum number of notes to return
    - offset: Number of notes to skip
    - sort: Optional sort option. Use '-created_at' for most recent first.
    """
    try:
        if sort == '-created_at':
            docs = await api.get_recent_documents(limit=limit)
        else:
            docs = await api.list_documents(limit=limit, offset=offset)
        return ndjson_response([build_document_dto(d) for d in docs])
    except Exception as e:
        raise _handle_error(e, 'Failed to list notes')


@router.post(
    '/notes/search',
    response_class=StreamingResponse,
    responses=ndjson_openapi(NoteSearchResult, 'Stream of note search results.'),
)
async def search_notes(
    request: Annotated[NoteSearchRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Search for notes using multi-query expansion and note-level fusion."""
    try:
        results = await api.search_documents(
            query=request.query,
            limit=request.limit,
            vault_ids=request.vault_ids,
            expand_query=request.expand_query,
            fusion_strategy=request.fusion_strategy,
            strategies=request.strategies,
            strategy_weights=request.strategy_weights,
            reason=request.reason,
            summarize=request.summarize,
        )
        return ndjson_response(results)
    except Exception as e:
        raise _handle_error(e, 'Note search failed')


@router.get('/notes/{document_id}/page-index')
async def get_note_page_index(document_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get the page index (slim tree) for a note."""
    try:
        page_index = await api.get_document_page_index(document_id)
        return {'document_id': document_id, 'page_index': page_index}
    except Exception as e:
        raise _handle_error(e, 'Failed to get page index')


@router.get('/notes/{document_id}', response_model=NoteDTO)
async def get_note(document_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get a note by ID."""
    try:
        doc = await api.get_document(document_id)
        return build_document_dto(doc)
    except Exception as e:
        raise _handle_error(e, 'Failed to get note')


@router.get('/nodes/{node_id}', response_model=NodeDTO)
async def get_node(node_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]) -> NodeDTO:
    """Get a specific note node by its ID."""
    try:
        node = await api.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f'Node {node_id} not found.')
        return node
    except HTTPException:
        raise
    except Exception as e:
        raise _handle_error(e, 'Failed to get node')


@router.delete('/notes/{document_id}')
async def delete_note(document_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete a note and all associated data (memory units, chunks, links, assets)."""
    try:
        await api.delete_document(document_id)
        return {'status': 'success'}
    except Exception as e:
        raise _handle_error(e, 'Note deletion failed')
