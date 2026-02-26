"""Shared utilities for route modules."""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from memex_common.exceptions import (
    AmbiguousResourceError,
    MemexError,
    ResourceNotFoundError,
    VaultNotFoundError,
)
from memex_common.schemas import NoteDTO, EntityDTO

from memex_core.api import MemexAPI

logger = logging.getLogger('memex.core.server')


def get_api(request: Request) -> MemexAPI:
    """Dependency to get the MemexAPI instance."""
    return request.app.state.api


def _handle_error(e: Exception, context: str) -> HTTPException:
    """Log the error explicitly and return an appropriate HTTPException."""
    if isinstance(e, HTTPException):
        raise e

    logger.error(f'{context}: {e}', exc_info=True)

    if isinstance(e, VaultNotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, ResourceNotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, AmbiguousResourceError):
        return HTTPException(status_code=400, detail=str(e))
    if isinstance(e, MemexError):
        return HTTPException(status_code=400, detail=str(e))

    return HTTPException(status_code=500, detail='Internal server error')


def _resolve_doc_name(metadata: dict[str, Any]) -> str | None:
    """Extract document name from metadata using the standard fallback chain."""
    return (
        metadata.get('name')
        or metadata.get('title')
        or metadata.get('retain_params', {}).get('note_name')
    )


def build_note_dto(doc: Any) -> NoteDTO:
    """Build a NoteDTO from an ORM object or a dict."""
    if isinstance(doc, dict):
        metadata = doc.get('doc_metadata') or {}
        doc_title = doc.get('title')
        return NoteDTO(
            id=doc['id'],
            title=doc_title,
            name=doc_title or _resolve_doc_name(metadata),
            original_text=doc.get('original_text'),
            created_at=doc['created_at'],
            vault_id=doc['vault_id'],
            doc_metadata=metadata,
        )

    metadata = doc.doc_metadata or {}
    doc_title = getattr(doc, 'title', None)
    return NoteDTO(
        id=doc.id,
        title=doc_title,
        name=doc_title or _resolve_doc_name(metadata),
        original_text=doc.original_text,
        created_at=doc.created_at,
        vault_id=doc.vault_id,
        doc_metadata=metadata,
    )


def build_entity_dto(entity: Any) -> EntityDTO:
    """Build an EntityDTO from an ORM entity object."""
    return EntityDTO(
        id=entity.id,
        name=entity.canonical_name,
        mention_count=entity.mention_count,
    )


def ndjson_response(items: Sequence[BaseModel | dict[str, Any]]) -> StreamingResponse:
    """Stream a pre-materialized sequence as newline-delimited JSON.

    Use this when the API method returns a ``list`` (the common case).
    The full result set is already in memory; this helper streams the
    *serialized* output so the HTTP response uses chunked transfer
    encoding, but it does **not** reduce peak memory usage.

    For true cursor-level streaming (large or unbounded result sets)
    where items are yielded lazily from the database, use
    :func:`async_ndjson_response` instead.
    """
    logger.debug('ndjson_response: streaming %d items', len(items))

    async def generate():
        for item in items:
            try:
                if isinstance(item, BaseModel):
                    yield item.model_dump_json() + '\n'
                else:

                    def default_converter(o: Any) -> Any:
                        if isinstance(o, BaseModel):
                            return o.model_dump(mode='json')
                        return str(o)

                    yield json.dumps(item, default=default_converter) + '\n'
            except Exception as e:
                logger.error('ndjson_response: failed to serialize item: %s', e)
                yield json.dumps({'error': str(e), 'type': 'serialization_error'}) + '\n'

    return StreamingResponse(generate(), media_type='application/x-ndjson')


async def async_ndjson_response(items: AsyncIterator[BaseModel]) -> StreamingResponse:
    """Stream an async iterator as newline-delimited JSON (true streaming).

    Use this when the API method returns an ``AsyncGenerator`` backed by a
    database cursor (e.g. ``session.stream()``).  Items are serialized and
    sent as they arrive, keeping peak memory proportional to a single item
    rather than the full result set.

    The connection is held open for the duration of the response; callers
    should ensure the underlying query uses ``READ ONLY`` where possible
    and that a reasonable timeout is configured on the connection pool.

    For pre-materialized lists, use :func:`ndjson_response` instead.
    """

    async def generate():
        async for item in items:
            try:
                yield item.model_dump_json() + '\n'
            except Exception as e:
                logger.error('async_ndjson_response: failed to serialize item: %s', e)
                yield json.dumps({'error': str(e), 'type': 'serialization_error'}) + '\n'

    return StreamingResponse(generate(), media_type='application/x-ndjson')


def ndjson_openapi(model: type[BaseModel], description: str) -> dict[int | str, dict[str, Any]]:
    """Generate OpenAPI response schema for an NDJSON streaming endpoint."""
    return {
        200: {
            'description': description,
            'content': {
                'application/x-ndjson': {
                    'schema': {
                        'type': 'string',
                        'description': (
                            f'Newline-delimited JSON. Each line is a {model.__name__} object.'
                        ),
                    }
                }
            },
        }
    }
