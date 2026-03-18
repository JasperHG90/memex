"""Key-value store and embedding endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from memex_common.exceptions import MemexError
from memex_common.schemas import KVEntryDTO, KVPutRequest, KVSearchRequest

from memex_core.api import MemexAPI
from memex_core.server.common import _handle_error, get_api

router = APIRouter(prefix='/api/v1')


class EmbedRequest(BaseModel):
    """Request to embed a text string."""

    text: str


class EmbedResponse(BaseModel):
    """Response with the embedding vector."""

    embedding: list[float]


@router.post('/embed', response_model=EmbedResponse)
async def embed_text(
    request: Annotated[EmbedRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Generate an embedding vector for the given text."""
    try:
        embedding = await api.embed_text(request.text)
        return EmbedResponse(embedding=embedding)
    except (MemexError, ValueError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to generate embedding')


@router.put('/kv', response_model=KVEntryDTO)
async def kv_put(
    request: Annotated[KVPutRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Create or update a key-value entry."""
    try:
        vault_id: UUID | None = None
        if request.vault_id is not None:
            vault_id = await api.resolve_vault_identifier(request.vault_id)

        if request.key is None:
            raise ValueError('key is required')

        entry = await api.kv_put(
            vault_id=vault_id,
            key=request.key,
            value=request.value,
            embedding=request.embedding,
        )
        return KVEntryDTO.model_validate(entry, from_attributes=True)
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to put KV entry')


@router.get('/kv/get', response_model=KVEntryDTO)
async def kv_get(
    api: Annotated[MemexAPI, Depends(get_api)],
    key: str = Query(description='Key to look up'),
    vault_id: str | None = Query(None, description='Vault ID or name'),
):
    """Get a key-value entry by key. Checks vault-specific first, then global."""
    try:
        resolved_vault_id: UUID | None = None
        if vault_id is not None:
            resolved_vault_id = await api.resolve_vault_identifier(vault_id)

        entry = await api.kv_get(key=key, vault_id=resolved_vault_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f'KV entry not found: {key}')
        return KVEntryDTO.model_validate(entry, from_attributes=True)
    except HTTPException:
        raise
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to get KV entry')


@router.post('/kv/search', response_model=list[KVEntryDTO])
async def kv_search(
    request: Annotated[KVSearchRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Semantic search over key-value entries by embedding similarity."""
    try:
        vault_id: UUID | None = None
        if request.vault_id is not None:
            vault_id = await api.resolve_vault_identifier(request.vault_id)

        # Embed the query text
        embeddings = api.embedding_model.encode([request.query])
        query_embedding = embeddings[0].tolist()

        entries = await api.kv_search(
            query_embedding=query_embedding,
            vault_id=vault_id,
            limit=request.limit,
        )
        return [KVEntryDTO.model_validate(e, from_attributes=True) for e in entries]
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'KV search failed')


@router.delete('/kv/delete')
async def kv_delete(
    api: Annotated[MemexAPI, Depends(get_api)],
    key: str = Query(description='Key to delete'),
    vault_id: str | None = Query(None, description='Vault ID or name'),
):
    """Delete a key-value entry."""
    try:
        resolved_vault_id: UUID | None = None
        if vault_id is not None:
            resolved_vault_id = await api.resolve_vault_identifier(vault_id)

        deleted = await api.kv_delete(key=key, vault_id=resolved_vault_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f'KV entry not found: {key}')
        return {'status': 'success'}
    except HTTPException:
        raise
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'KV deletion failed')


@router.get('/kv', response_model=list[KVEntryDTO])
async def kv_list(
    api: Annotated[MemexAPI, Depends(get_api)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    vault_id: str | None = Query(None, description='Vault ID or name'),
    exclude_prefix: str | None = Query(
        None, description='Exclude entries whose key starts with this prefix'
    ),
    key_prefix: str | None = Query(
        None, description='Only include entries whose key starts with this prefix'
    ),
):
    """List key-value entries. Without vault_id returns global only; with vault_id returns both."""
    try:
        resolved_vault_id: UUID | None = None
        if vault_id is not None:
            resolved_vault_id = await api.resolve_vault_identifier(vault_id)

        entries = await api.kv_list(
            vault_id=resolved_vault_id,
            limit=limit,
            exclude_prefix=exclude_prefix,
            key_prefix=key_prefix,
        )
        return [KVEntryDTO.model_validate(e, from_attributes=True) for e in entries]
    except (MemexError, ValueError, KeyError, RuntimeError, OSError) as e:
        raise _handle_error(e, 'Failed to list KV entries')
