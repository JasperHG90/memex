"""Shared utilities for route modules."""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from memex_common.exceptions import (
    AmbiguousResourceError,
    AppendIdConflictError,
    AppendLockTimeoutError,
    DeltaValidationError,
    FeatureDisabledError,
    MemexError,
    NoteNotAppendableError,
    ObservationReadOnlyError,
    ResourceNotFoundError,
    VaultNotFoundError,
)
from memex_common.schemas import (
    EntityDTO,
    IntentClass,
    MemoryUnitDTO,
    NoteDTO,
    NoteListItemDTO,
    RiskClass,
    StrategyDebugInfo,
    SupersessionInfo,
)

from memex_core.api import MemexAPI
from memex_core.context import get_session_id
from memex_core.metrics import DTO_ENUM_COERCION_TOTAL

logger = logging.getLogger('memex.core.server')

# Exception types whose 4xx responses ride a high-volume polling pattern
# (hermes-plugin's 10 Hz readback). Logging these at ERROR with full
# tracebacks floods the log and obscures real incidents; demote to INFO.
# ObservationReadOnlyError is intentionally excluded by the inline guard
# in `_handle_error` — defense in depth so an inheritance refactor doesn't
# silently demote a typed 400-detail response. Add a new type here only
# after confirming its volume and that an INFO-level log is sufficient
# for ops visibility.
_DEMOTE_TO_INFO_TYPES: tuple[type[Exception], ...] = (
    ResourceNotFoundError,
    VaultNotFoundError,
    AmbiguousResourceError,
)


def get_api(request: Request) -> MemexAPI:
    """Dependency to get the MemexAPI instance."""
    return request.app.state.api


async def resolve_vault_ids(api: MemexAPI, identifiers: list[str] | None) -> list[UUID] | None:
    """Resolve a list of vault identifiers (UUIDs or names) to UUIDs."""
    if not identifiers:
        return None
    return [await api.resolve_vault_identifier(v) for v in identifiers]


def _handle_error(e: Exception, context: str) -> HTTPException:
    """Log the error explicitly and return an appropriate HTTPException."""
    if isinstance(e, HTTPException):
        raise e

    # Log-level: demote high-volume client errors to INFO (see
    # _DEMOTE_TO_INFO_TYPES). `exc_info=e` (vs True) pulls the traceback from
    # the passed exception rather than `sys.exc_info()`, so the log line
    # carries the right stack even if a future caller routes here outside
    # an active `except` block.
    if isinstance(e, _DEMOTE_TO_INFO_TYPES) and not isinstance(e, ObservationReadOnlyError):
        logger.info(f'{context}: {e}')
    else:
        logger.error(f'{context}: {e}', exc_info=e)

    # ObservationReadOnlyError must precede every other isinstance check.
    # It is a MemexError subclass; the generic `isinstance(e, MemexError)`
    # branch below would otherwise flatten its structured `source_memory_units`
    # detail to a string and silently break the agent contract. Defense in
    # depth: route handlers also catch this explicitly, but this clause
    # closes the gap for any future call site that routes through
    # `_handle_error` directly.
    if isinstance(e, ObservationReadOnlyError):
        # Shape owned by ObservationReadOnlyError.to_http_detail() — same
        # SSOT as the explicit route handler in server/memories.py.
        return HTTPException(status_code=400, detail=e.to_http_detail())

    if isinstance(e, VaultNotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, ResourceNotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, AmbiguousResourceError):
        return HTTPException(status_code=400, detail=str(e))
    if isinstance(e, (AppendIdConflictError, NoteNotAppendableError)):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, AppendLockTimeoutError):
        return HTTPException(
            status_code=503,
            detail=str(e),
            headers={'Retry-After': '5'},
        )
    if isinstance(e, FeatureDisabledError):
        return HTTPException(status_code=503, detail=str(e))
    if isinstance(e, DeltaValidationError):
        return HTTPException(status_code=422, detail=str(e))
    if isinstance(e, MemexError):
        return HTTPException(status_code=400, detail=str(e))

    correlation_id = get_session_id()
    return HTTPException(
        status_code=500,
        detail={'error': 'Internal server error', 'correlation_id': correlation_id},
    )


def _resolve_doc_name(metadata: dict[str, Any]) -> str | None:
    """Extract document name from metadata using the standard fallback chain."""
    return (
        metadata.get('name')
        or metadata.get('title')
        or metadata.get('retain_params', {}).get('note_name')
    )


def _resolve_description(doc: Any) -> str | None:
    """Extract description from the dedicated column, falling back to retain_params."""
    if isinstance(doc, dict):
        desc = doc.get('description')
        if not desc:
            desc = (doc.get('doc_metadata') or {}).get('retain_params', {}).get('note_description')
        return desc
    desc = getattr(doc, 'description', None)
    if not desc:
        metadata = getattr(doc, 'doc_metadata', None) or {}
        desc = metadata.get('retain_params', {}).get('note_description')
    return desc


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
            publish_date=doc.get('publish_date'),
            vault_id=doc['vault_id'],
            vault_name=doc.get('vault_name'),
            description=_resolve_description(doc),
            assets=doc.get('assets', []),
            doc_metadata=metadata,
            template=metadata.get('template'),
            status=doc.get('status', 'active'),
            archived_at=doc.get('archived_at'),
        )

    metadata = doc.doc_metadata or {}
    doc_title = getattr(doc, 'title', None)
    return NoteDTO(
        id=doc.id,
        title=doc_title,
        name=doc_title or _resolve_doc_name(metadata),
        original_text=doc.original_text,
        created_at=doc.created_at,
        publish_date=getattr(doc, 'publish_date', None),
        vault_id=doc.vault_id,
        vault_name=getattr(doc, 'vault_name', None),
        description=_resolve_description(doc),
        assets=getattr(doc, 'assets', []) or [],
        doc_metadata=metadata,
        template=metadata.get('template'),
        status=getattr(doc, 'status', 'active'),
        archived_at=getattr(doc, 'archived_at', None),
    )


def build_note_list_item_dto(doc: Any) -> NoteListItemDTO:
    """Build a NoteListItemDTO from an ORM object — summaries instead of full text."""
    metadata = getattr(doc, 'doc_metadata', None) or {}
    doc_title = getattr(doc, 'title', None)
    return NoteListItemDTO(
        id=doc.id,
        title=doc_title,
        name=doc_title or _resolve_doc_name(metadata),
        created_at=doc.created_at,
        publish_date=getattr(doc, 'publish_date', None),
        vault_id=doc.vault_id,
        vault_name=getattr(doc, 'vault_name', None),
        description=_resolve_description(doc),
        assets=getattr(doc, 'assets', []) or [],
        doc_metadata=metadata,
        template=metadata.get('template'),
        summaries=getattr(doc, 'summaries', []),
        status=getattr(doc, 'status', 'active'),
        archived_at=getattr(doc, 'archived_at', None),
    )


def build_entity_dto(entity: Any) -> EntityDTO:
    """Build an EntityDTO from an ORM entity object or EntityWithMetadata wrapper.

    Accepts either an ``EntityWithMetadata`` (preferred) or a plain ORM entity
    (backward-compatible, produces empty metadata).
    """
    from memex_core.services.entities import EntityWithMetadata

    if isinstance(entity, EntityWithMetadata):
        metadata = entity.metadata or {}
        orm_entity = entity.entity
    else:
        metadata = {}
        orm_entity = entity

    return EntityDTO(
        id=orm_entity.id,
        name=orm_entity.canonical_name,
        mention_count=orm_entity.mention_count,
        entity_type=getattr(orm_entity, 'entity_type', None),
        metadata=metadata,
    )


def _coerce_intent_class(value: Any) -> IntentClass:
    """Coerce a raw intent_class string to an IntentClass enum.

    SQL CHECK constraint at sql_models.py blocks invalid writes today; this
    is defense-in-depth for future schema drift. Unrecognised values are
    mapped to DURABLE with a warning + Prometheus counter increment.
    """
    if value is None:
        return IntentClass.DURABLE
    try:
        return IntentClass(value)
    except (ValueError, TypeError):
        logger.warning('Unrecognised intent_class %r in DTO ctor → durable.', value)
        DTO_ENUM_COERCION_TOTAL.labels(field='intent_class', reason='invalid').inc()
        return IntentClass.DURABLE


def _coerce_risk_class(value: Any) -> RiskClass:
    if value is None:
        return RiskClass.NONE
    try:
        return RiskClass(value)
    except (ValueError, TypeError):
        logger.warning('Unrecognised risk_class %r in DTO ctor → none.', value)
        DTO_ENUM_COERCION_TOTAL.labels(field='risk_class', reason='invalid').inc()
        return RiskClass.NONE


def _extract_superseded_by(unit: Any) -> list[SupersessionInfo] | None:
    """Read supersession entries from unit.unit_metadata['superseded_by'].

    Populated only by the search engine (engine.py:1340-1362) which writes a
    list of dicts ``{unit_id, unit_text, note_title, relation}``. Single-unit
    fetch endpoints don't populate this — None is the correct default there.
    """
    meta = getattr(unit, 'unit_metadata', None) or {}
    raw = meta.get('superseded_by') if isinstance(meta, dict) else None
    if not raw:
        return None
    out: list[SupersessionInfo] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(SupersessionInfo(**entry))
        except (ValueError, TypeError) as exc:
            logger.warning('Skipping malformed supersession entry: %s', exc)
    return out or None


def build_memory_unit_dto(
    unit: Any,
    *,
    debug: bool = False,
) -> MemoryUnitDTO:
    """Build a MemoryUnitDTO from a MemoryUnit ORM/model object.

    Handles all field variations across retrieval, mentions, and single-unit
    endpoints.  Optional ``debug`` flag controls whether per-strategy
    attribution data is included.

    Confidence-variance is intentionally NOT passed: the DTO defaults to
    _MAX_VARIANCE, preserving the cold-start invariant (variance derived
    from confidence + evidence_count via mean_and_variance, not stored).
    """
    doc_id = getattr(unit, 'note_id', None)
    source_docs: list[UUID] = [doc_id] if doc_id else []

    debug_info: list[StrategyDebugInfo] | None = None
    if debug:
        raw_debug = getattr(unit, '_debug_info', None)
        if raw_debug:
            debug_info = [
                StrategyDebugInfo(
                    strategy_name=c.strategy_name,
                    rank=c.rank,
                    rrf_score=c.rrf_score,
                    raw_score=c.raw_score,
                    timing_ms=c.timing_ms,
                )
                for c in raw_debug
            ]

    return MemoryUnitDTO(
        id=unit.id,
        note_id=doc_id,
        source_note_ids=source_docs,
        text=unit.text,
        fact_type=unit.fact_type,
        status=unit.status,
        mentioned_at=unit.mentioned_at or getattr(unit, 'event_date', None),
        occurred_start=unit.occurred_start,
        occurred_end=unit.occurred_end,
        vault_id=unit.vault_id,
        metadata=unit.unit_metadata,
        score=getattr(unit, 'score', None),
        chunk_id=getattr(unit, 'chunk_id', None),
        confidence=getattr(unit, 'confidence', 1.0) or 1.0,
        confidence_evidence_count=getattr(unit, 'confidence_evidence_count', 0) or 0,
        intent_class=_coerce_intent_class(getattr(unit, 'intent_class', None)),
        risk_class=_coerce_risk_class(getattr(unit, 'risk_class', None)),
        last_outcome_at=getattr(unit, 'last_outcome_at', None),
        success_co_count=getattr(unit, 'success_co_count', 0) or 0,
        failure_co_count=getattr(unit, 'failure_co_count', 0) or 0,
        is_deprioritized=bool(getattr(unit, 'is_deprioritized', False)),
        superseded_by=_extract_superseded_by(unit),
        debug_info=debug_info,
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
            except (TypeError, ValueError, AttributeError) as e:
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
            except (TypeError, ValueError, AttributeError) as e:
                logger.error('async_ndjson_response: failed to serialize item: %s', e)
                yield json.dumps({'error': str(e), 'type': 'serialization_error'}) + '\n'

    return StreamingResponse(generate(), media_type='application/x-ndjson')


def ndjson_openapi(model: type[BaseModel], description: str) -> dict[int | str, dict[str, Any]]:
    """Generate OpenAPI response schema for an NDJSON streaming endpoint.

    Produces a schema where each line is a JSON object matching ``model``.
    On serialization errors an error line ``{"error": "...", "type": "serialization_error"}``
    may appear in the stream.
    """
    try:
        item_schema: dict[str, Any] = model.model_json_schema()
    except AttributeError:
        item_schema = {'type': 'object', 'description': f'{model.__name__} object'}
    error_schema: dict[str, Any] = {
        'type': 'object',
        'properties': {
            'error': {'type': 'string'},
            'type': {'type': 'string', 'enum': ['serialization_error']},
        },
        'required': ['error', 'type'],
    }
    return {
        200: {
            'description': (
                f'{description} '
                f'Each line is a JSON-encoded `{model.__name__}` object. '
                'On serialization failure a line with `"type": "serialization_error"` is emitted.'
            ),
            'content': {
                'application/x-ndjson': {
                    'schema': {
                        'type': 'string',
                        'description': (
                            'Newline-delimited JSON stream. Each line is one of the schemas below.'
                        ),
                        'x-ndjson-line-schema': {
                            'oneOf': [
                                item_schema,
                                error_schema,
                            ]
                        },
                    }
                }
            },
        }
    }
