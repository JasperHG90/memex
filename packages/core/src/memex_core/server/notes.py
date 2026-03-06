"""Note endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

from memex_common.exceptions import MemexError
from memex_common.schemas import NoteDTO, NoteSearchRequest, NoteSearchResult, NodeDTO

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    build_note_dto,
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
    vault_id: list[UUID] | None = Query(None, description='Filter by vault ID(s)'),
):
    """
    List notes.

    Query params:
    - limit: Maximum number of notes to return
    - offset: Number of notes to skip
    - sort: Optional sort option. Use '-created_at' for most recent first.
    - vault_id: Optional vault ID(s) to filter by.
    """
    try:
        if sort == '-created_at':
            docs = await api.get_recent_notes(limit=limit, vault_ids=vault_id)
        else:
            docs = await api.list_notes(limit=limit, offset=offset, vault_ids=vault_id)
        return ndjson_response([build_note_dto(d) for d in docs])
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
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
        results = await api.search_notes(
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
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Note search failed')


@router.get('/notes/{note_id}/page-index')
async def get_note_page_index(note_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get the page index (slim tree) for a note."""
    try:
        page_index = await api.get_note_page_index(note_id)
        return {'note_id': note_id, 'page_index': page_index}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get page index')


@router.get('/notes/{note_id}/metadata')
async def get_note_metadata(note_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get just the metadata from a note's page index."""
    try:
        metadata = await api.get_note_metadata(note_id)
        return {'note_id': note_id, 'metadata': metadata}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get note metadata')


@router.get('/notes/{note_id}', response_model=NoteDTO)
async def get_note(note_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get a note by ID."""
    try:
        doc = await api.get_note(note_id)
        return build_note_dto(doc)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
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
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get node')


class SetNoteStatusRequest(BaseModel):
    status: str
    linked_note_id: UUID | None = None


@router.patch('/notes/{note_id}/status')
async def set_note_status(
    note_id: UUID,
    request: Annotated[SetNoteStatusRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Set note lifecycle status (active, superseded, appended)."""
    try:
        result = await api.set_note_status(note_id, request.status, request.linked_note_id)
        return result
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to set note status')


class RenameNoteRequest(BaseModel):
    new_title: str


@router.patch('/notes/{note_id}/title')
async def rename_note(
    note_id: UUID,
    request: Annotated[RenameNoteRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Rename a note (updates title in metadata, page index, and doc_metadata)."""
    try:
        await api.update_note_title(note_id, request.new_title)
        return {'status': 'success', 'note_id': str(note_id), 'new_title': request.new_title}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to rename note')


@router.delete('/notes/{note_id}')
async def delete_note(note_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete a note and all associated data (memory units, chunks, links, assets)."""
    try:
        await api.delete_note(note_id)
        return {'status': 'success'}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Note deletion failed')


class MigrateNoteRequest(BaseModel):
    target_vault_id: str


@router.post('/notes/{note_id}/migrate')
async def migrate_note(
    note_id: UUID,
    request: Annotated[MigrateNoteRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Move a note and all associated data to a different vault."""
    try:
        result = await api.migrate_note(note_id, request.target_vault_id)
        return result
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Note migration failed')
