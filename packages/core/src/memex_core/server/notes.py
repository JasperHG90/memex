"""Note endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
)
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

from memex_common.exceptions import MemexError
from memex_common.schemas import (
    FindNoteResult,
    MemoryLinkDTO,
    NoteDTO,
    NoteListItemDTO,
    NoteSearchRequest,
    NoteSearchResult,
    NodeDTO,
)

from memex_core.api import MemexAPI
from memex_core.server.auth import (
    AuthContext,
    check_vault_access,
    get_auth_context,
    require_delete,
    require_read,
    require_write,
)
from memex_core.server.common import (
    _handle_error,
    build_note_dto,
    build_note_list_item_dto,
    get_api,
    ndjson_openapi,
    ndjson_response,
    resolve_vault_ids,
)

router = APIRouter(prefix='/api/v1')


@router.get(
    '/notes',
    response_class=StreamingResponse,
    responses=ndjson_openapi(NoteListItemDTO, 'Stream of notes with summaries.'),
    dependencies=[Depends(require_read)],
)
async def list_notes(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Literal['-created_at'] | None = Query(
        None, description='Sort option: -created_at for recency'
    ),
    vault_id: list[str] | None = Query(None, description='Filter by vault ID(s) or name(s)'),
    after: str | None = Query(None, description='Only notes on or after this date (ISO 8601).'),
    before: str | None = Query(None, description='Only notes on or before this date (ISO 8601).'),
    template: str | None = Query(
        None, description='Filter by template slug (e.g. "general_note").'
    ),
    tags: list[str] | None = Query(
        None, description='Filter by tags (AND semantics). Only notes containing all tags.'
    ),
    status: str | None = Query(
        None,
        description='Filter by note lifecycle status (e.g. "active", "archived").',
    ),
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
):
    """
    List notes.

    Query params:
    - limit: Maximum number of notes to return
    - offset: Number of notes to skip
    - sort: Optional sort option. Use '-created_at' for most recent first.
    - vault_id: Optional vault ID(s) or name(s) to filter by.
    - after: ISO 8601 date string. Only notes with date >= after.
    - before: ISO 8601 date string. Only notes with date <= before.
    - tags: Filter by tags (AND semantics).
    - status: Filter by note lifecycle status.
    """
    from datetime import datetime as dt

    parsed_after = None
    parsed_before = None
    try:
        if after is not None:
            parsed_after = dt.fromisoformat(after)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f'Invalid "after" date format: {exc}')
    try:
        if before is not None:
            parsed_before = dt.fromisoformat(before)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f'Invalid "before" date format: {exc}')

    try:
        await check_vault_access(auth, vault_id, api)
        resolved = await resolve_vault_ids(api, vault_id)
        if sort == '-created_at':
            docs = await api.get_recent_notes(
                limit=limit,
                vault_ids=resolved,
                after=parsed_after,
                before=parsed_before,
                template=template,
            )
        else:
            docs = await api.list_notes(
                limit=limit,
                offset=offset,
                vault_ids=resolved,
                after=parsed_after,
                before=parsed_before,
                template=template,
                tags=tags,
                status=status,
            )
        return ndjson_response([build_note_list_item_dto(d) for d in docs])
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to list notes')


@router.post(
    '/notes/search',
    response_class=StreamingResponse,
    responses=ndjson_openapi(NoteSearchResult, 'Stream of note search results.'),
    dependencies=[Depends(require_read)],
)
async def search_notes(
    request: Annotated[NoteSearchRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
):
    """Search for notes using multi-query expansion and note-level fusion."""
    try:
        await check_vault_access(auth, request.vault_ids, api)
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
            mmr_lambda=request.mmr_lambda,
            after=request.after,
            before=request.before,
            tags=request.tags,
        )
        return ndjson_response(results)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Note search failed')


class RelatedNotesRequest(BaseModel):
    note_ids: list[UUID]


@router.post('/notes/related', dependencies=[Depends(require_read)])
async def get_related_notes(
    request: RelatedNotesRequest,
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Get notes related to the given notes via shared entities."""
    try:
        related_map = await api.get_related_notes(request.note_ids)
        return {str(k): [v.model_dump(mode='json') for v in vs] for k, vs in related_map.items()}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Related notes lookup failed')


@router.get(
    '/notes/find', response_model=list[FindNoteResult], dependencies=[Depends(require_read)]
)
async def find_notes_by_title(
    api: Annotated[MemexAPI, Depends(get_api)],
    query: str = Query(..., description='Title search query'),
    vault_id: list[str] | None = Query(None, description='Filter by vault ID(s) or name(s)'),
    limit: Annotated[int, Query(ge=1, le=500, description='Maximum results to return')] = 5,
    auth: Annotated[AuthContext | None, Depends(get_auth_context)] = None,
):
    """Fuzzy-search notes by title using trigram similarity."""
    try:
        await check_vault_access(auth, vault_id, api)
        resolved = await resolve_vault_ids(api, vault_id)
        results = await api.find_notes_by_title(query=query, vault_ids=resolved, limit=limit)
        return [FindNoteResult(**r) for r in results]
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to find notes by title')


@router.get('/notes/{note_id}/page-index', dependencies=[Depends(require_read)])
async def get_note_page_index(note_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get the page index (slim tree) for a note."""
    try:
        page_index = await api.get_note_page_index(note_id)
        return {'note_id': note_id, 'page_index': page_index}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get page index')


@router.get('/notes/{note_id}/metadata', dependencies=[Depends(require_read)])
async def get_note_metadata(note_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get just the metadata from a note's page index."""
    try:
        metadata = await api.get_note_metadata(note_id)
        return {'note_id': note_id, 'metadata': metadata}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get note metadata')


@router.get('/notes/{note_id}', response_model=NoteDTO, dependencies=[Depends(require_read)])
async def get_note(note_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get a note by ID."""
    try:
        doc = await api.get_note(note_id)
        return build_note_dto(doc)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get note')


class BatchNodeRequest(BaseModel):
    node_ids: list[UUID]


@router.post('/nodes/batch', response_model=list[NodeDTO], dependencies=[Depends(require_read)])
async def get_nodes_batch(
    request: Annotated[BatchNodeRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
) -> list[NodeDTO]:
    """Get multiple note nodes by ID."""
    try:
        return await api.get_nodes(request.node_ids)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get nodes batch')


class BatchNoteMetadataRequest(BaseModel):
    note_ids: list[UUID]


@router.post('/notes/metadata/batch', dependencies=[Depends(require_read)])
async def get_notes_metadata_batch(
    request: Annotated[BatchNoteMetadataRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Get metadata for multiple notes."""
    try:
        return await api.get_notes_metadata(request.note_ids)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get notes metadata batch')


@router.get('/nodes/{node_id}', response_model=NodeDTO, dependencies=[Depends(require_read)])
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


@router.patch('/notes/{note_id}/status', dependencies=[Depends(require_write)])
async def set_note_status(
    note_id: UUID,
    request: Annotated[SetNoteStatusRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Set note lifecycle status (active, superseded, appended)."""
    try:
        return await api.set_note_status(note_id, request.status, request.linked_note_id)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to set note status')


class UpdateNoteDateRequest(BaseModel):
    date: str


@router.patch('/notes/{note_id}/date', dependencies=[Depends(require_write)])
async def update_note_date(
    note_id: UUID,
    request: Annotated[UpdateNoteDateRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Update a note's publish_date and cascade delta to all memory unit timestamps."""
    from datetime import datetime

    try:
        new_date = datetime.fromisoformat(request.date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f'Invalid date format: {request.date!r}')

    try:
        return await api.update_note_date(note_id, new_date)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to update note date')


class RenameNoteRequest(BaseModel):
    new_title: str


@router.patch('/notes/{note_id}/title', dependencies=[Depends(require_write)])
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


@router.delete('/notes/{note_id}', dependencies=[Depends(require_delete)])
async def delete_note(
    note_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Delete a note and all associated data (memory units, chunks, links, assets)."""
    try:
        await api.delete_note(note_id)
        return {'status': 'success'}
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Note deletion failed')


class MigrateNoteRequest(BaseModel):
    target_vault_id: str


@router.post('/notes/{note_id}/migrate', dependencies=[Depends(require_write)])
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


@router.post('/notes/{note_id}/assets', dependencies=[Depends(require_write)])
async def add_note_assets(
    note_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    files: list[UploadFile] = File(...),
):
    """Add one or more asset files to an existing note."""
    try:
        file_dict: dict[str, bytes] = {}
        for upload_file in files:
            filename = upload_file.filename or 'unnamed'
            content = await upload_file.read()
            file_dict[filename] = content

        result = await api.add_note_assets(note_id, file_dict)
        return result
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to add assets to note')


class DeleteNoteAssetsRequest(BaseModel):
    asset_paths: list[str]


@router.delete('/notes/{note_id}/assets', dependencies=[Depends(require_delete)])
async def delete_note_assets(
    note_id: UUID,
    request: Annotated[DeleteNoteAssetsRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Delete one or more asset files from an existing note."""
    try:
        result = await api.delete_note_assets(note_id, request.asset_paths)
        return result
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to delete assets from note')


class UpdateUserNotesRequest(BaseModel):
    user_notes: str | None


@router.patch('/notes/{note_id}/user-notes', dependencies=[Depends(require_write)])
async def update_user_notes(
    note_id: UUID,
    request: Annotated[UpdateUserNotesRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Update user_notes on an existing note and reprocess into memory graph."""
    try:
        return await api.update_user_notes(note_id, request.user_notes)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to update user notes')


@router.get(
    '/notes/{note_id}/links',
    response_model=list[MemoryLinkDTO],
    dependencies=[Depends(require_read)],
)
async def get_note_links(
    note_id: UUID,
    api: Annotated[MemexAPI, Depends(get_api)],
    link_type: str | None = Query(None, description='Filter by link type (e.g. contradicts).'),
    limit: int = Query(20, ge=1, le=200, description='Max links to return.'),
) -> list[MemoryLinkDTO]:
    """Get typed relationship links for a note (aggregated from its memory units)."""
    try:
        link_types = [link_type] if link_type else None
        links_map = await api.get_note_links([note_id], link_types=link_types, limit=limit)
        return links_map.get(note_id, [])
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, f'Failed to get links for note {note_id}')
