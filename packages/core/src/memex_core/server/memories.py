"""Memory unit endpoints."""

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse

from memex_common.schemas import (
    AdjustBeliefRequest,
    MemoryUnitDTO,
    RetrievalRequest,
    SummaryRequest,
    SummaryResponse,
)
from memex_common.types import FactTypes

from memex_core.api import MemexAPI
from memex_core.server.common import (
    _handle_error,
    get_api,
    ndjson_openapi,
    ndjson_response,
)

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1')


def _collect_evidence_ids(units: list[Any]) -> set[UUID]:
    """Collect all evidence IDs from opinion-type memory units."""
    evidence_ids: set[UUID] = set()
    for u in units:
        ft = FactTypes(u.fact_type) if isinstance(u.fact_type, str) else u.fact_type
        if ft == FactTypes.OPINION:
            indices = u.unit_metadata.get('evidence_indices', [])
            for idx in indices:
                try:
                    evidence_ids.add(UUID(str(idx)))
                except (ValueError, TypeError):
                    pass
    return evidence_ids


def _build_retrieval_dtos(
    units: list[Any],
    evidence_doc_map: dict[UUID, UUID],
) -> list[MemoryUnitDTO]:
    """Convert memory units to DTOs with resolved source document lineage."""
    dtos = []
    for u in units:
        source_docs: list[UUID] = []

        # A. Direct Document ID (Facts)
        doc_id = getattr(u, 'document_id', None)
        if doc_id:
            source_docs.append(doc_id)

        # B. Indirect Document IDs (Opinions via Evidence)
        ft = FactTypes(u.fact_type) if isinstance(u.fact_type, str) else u.fact_type
        if ft == FactTypes.OPINION:
            indices = u.unit_metadata.get('evidence_indices', [])
            for idx in indices:
                try:
                    uid = UUID(str(idx))
                    if uid in evidence_doc_map:
                        source_docs.append(evidence_doc_map[uid])
                except (ValueError, TypeError):
                    pass

        # Deduplicate source docs
        source_docs = list(set(source_docs))

        dtos.append(
            MemoryUnitDTO(
                id=u.id,
                document_id=doc_id,
                source_note_ids=source_docs,
                text=u.text,
                fact_type=ft,
                status=u.status,
                mentioned_at=u.mentioned_at or u.event_date,
                occurred_start=u.occurred_start,
                occurred_end=u.occurred_end,
                vault_id=u.vault_id,
                metadata=u.unit_metadata,
                score=getattr(u, 'score', None),
            )
        )
    return dtos


@router.post(
    '/memories/search',
    response_class=StreamingResponse,
    responses=ndjson_openapi(MemoryUnitDTO, 'Stream of memory units with resolved lineage.'),
)
async def search_memories(
    request: Annotated[RetrievalRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
    background_tasks: BackgroundTasks,
):
    """Search for memories."""
    try:
        units = await api.search(
            query=request.query,
            limit=request.limit,
            # NB: opinion formation is handled in background task below
            skip_opinion_formation=True,
            vault_ids=request.vault_ids,
            token_budget=request.token_budget,
            strategies=request.strategies,
            include_stale=request.include_stale,
        )

        if not request.skip_opinion_formation and request.strategies is None and units:
            try:
                target_vault_id = await api.resolve_vault_identifier(api.config.server.active_vault)
                # Pass only minimal context to avoid memory leak from holding full units.
                # Background tasks retain references to their arguments, keeping the entire
                # MemoryUnit object graph alive until the task completes.
                minimal_context = [
                    {
                        'id': str(u.id),
                        'text': u.text,
                        'fact_type': str(u.fact_type) if u.fact_type else None,
                        'formatted_fact_text': u.formatted_fact_text
                        if hasattr(u, 'formatted_fact_text')
                        else u.text,
                    }
                    for u in units
                ]
                background_tasks.add_task(
                    api.process_opinion_formation_minimal,
                    query=request.query,
                    context=minimal_context,
                    vault_id=target_vault_id,
                )
            except Exception as e:
                logger.warning(f'Failed to schedule background opinion formation: {e}')

        # Lineage Resolution
        evidence_to_resolve = _collect_evidence_ids(units)

        evidence_doc_map: dict[UUID, UUID] = {}
        if evidence_to_resolve:
            evidence_doc_map = await api.resolve_source_documents(list(evidence_to_resolve))

        return ndjson_response(_build_retrieval_dtos(units, evidence_doc_map))
    except Exception as e:
        raise _handle_error(e, 'Memory search failed')


@router.post(
    '/memories/summary',
    response_model=SummaryResponse,
    summary='Summarize search results',
    description='Generate an AI summary with citations from search result texts.',
)
async def summarize_memories(
    request: Annotated[SummaryRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
) -> SummaryResponse:
    """Synthesize search results into a concise answer with citations."""
    try:
        summary = await api.summarize_search_results(
            query=request.query,
            texts=request.texts,
        )
        return SummaryResponse(summary=summary)
    except Exception as e:
        raise _handle_error(e, 'Summary generation failed')


@router.patch('/memories/{unit_uuid}/belief')
async def adjust_memory_belief(
    unit_uuid: UUID,
    request: Annotated[AdjustBeliefRequest, Body()],
    api: Annotated[MemexAPI, Depends(get_api)],
):
    """Adjust belief confidence for a memory unit."""
    try:
        await api.adjust_belief(
            unit_uuid=unit_uuid,
            evidence_type_key=request.evidence_type_key,
            description=request.description,
        )
        return {'status': 'success'}
    except Exception as e:
        raise _handle_error(e, 'Belief adjustment failed')


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
