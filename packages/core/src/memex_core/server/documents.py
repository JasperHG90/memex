"""Document endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse

from memex_common.schemas import DocumentDTO, DocumentSearchRequest, DocumentSearchResult, NodeDTO

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
    '/documents/recent',
    response_class=StreamingResponse,
    responses=ndjson_openapi(DocumentDTO, 'Stream of recent documents.'),
)
async def get_recent_documents(api: Annotated[MemexAPI, Depends(get_api)], limit: int = 5):
    """Get the most recent documents."""
    try:
        docs = await api.get_recent_documents(limit=limit)
        return ndjson_response([build_document_dto(d) for d in docs])
    except Exception as e:
        raise _handle_error(e, 'Failed to fetch recent documents')


@router.get(
    '/documents',
    response_class=StreamingResponse,
    responses=ndjson_openapi(DocumentDTO, 'Stream of documents.'),
)
async def list_documents(
    api: Annotated[MemexAPI, Depends(get_api)], limit: int = 100, offset: int = 0
):
    """List documents."""
    try:
        docs = await api.list_documents(limit=limit, offset=offset)
        return ndjson_response([build_document_dto(d) for d in docs])
    except Exception as e:
        raise _handle_error(e, 'Failed to list documents')


@router.post(
    '/documents/search',
    response_class=StreamingResponse,
    responses=ndjson_openapi(DocumentSearchResult, 'Stream of document search results.'),
)
async def search_documents(
    request: Annotated[DocumentSearchRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Search for documents using multi-query expansion and document-level fusion."""
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
        raise _handle_error(e, 'Document search failed')


@router.get('/documents/{doc_id}/page_index')
async def get_document_page_index(doc_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get the page index (slim tree) for a document."""
    try:
        page_index = await api.get_document_page_index(doc_id)
        return {'document_id': doc_id, 'page_index': page_index}
    except Exception as e:
        raise _handle_error(e, 'Failed to get page index')


@router.get('/documents/{doc_id}', response_model=DocumentDTO)
async def get_document(doc_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Get a document by ID."""
    try:
        doc = await api.get_document(doc_id)
        return build_document_dto(doc)
    except Exception as e:
        raise _handle_error(e, 'Failed to get document')


@router.get('/nodes/{node_id}', response_model=NodeDTO)
async def get_node(node_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]) -> NodeDTO:
    """Get a specific document node by its ID."""
    try:
        node = await api.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f'Node {node_id} not found.')
        return node
    except HTTPException:
        raise
    except Exception as e:
        raise _handle_error(e, 'Failed to get node')


@router.delete('/documents/{document_id}')
async def delete_document(document_id: UUID, api: Annotated[MemexAPI, Depends(get_api)]):
    """Delete a document and all associated data (memory units, chunks, links, assets)."""
    try:
        await api.delete_document(document_id)
        return {'status': 'success'}
    except Exception as e:
        raise _handle_error(e, 'Document deletion failed')
