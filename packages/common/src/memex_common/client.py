"""
Remote client for the Memex API.
Used by the CLI to interact with a running Memex server.
"""

import datetime as dt
import logging
from typing import Any, AsyncGenerator
from uuid import UUID

import httpx
from pydantic import BaseModel

from memex_common.vault_utils import resolve_vault_list
from memex_common.schemas import (
    RetrievalRequest,
    ReflectionRequest,
    IngestURLRequest,
    IngestFileRequest,
    BatchIngestRequest,
    BatchJobStatus,
    CreateVaultRequest,
    DeadLetterItemDTO,
    DefaultVaultsResponse,
    FindNoteResult,
    MemoryLinkDTO,
    NoteCreateDTO,
    ReflectionResultDTO,
    MemoryUnitDTO,
    VaultDTO,
    VaultSummaryDTO,
    ReflectionQueueDTO,
    IngestResponse,
    EntityDTO,
    KVEntryDTO,
    KVPutRequest,
    KVSearchRequest,
    LineageResponse,
    LineageDirection,
    SystemStatsCountsDTO,
    NoteDTO,
    NoteListItemDTO,
    NoteSearchResult,
    NoteSearchRequest,
    NodeDTO,
    SummaryRequest,
    SummaryResponse,
    SurveyRequest,
    SurveyResponse,
)

logger = logging.getLogger('memex.common.client')


class RemoteMemexAPI:
    """
    Client for interacting with a remote Memex server via REST.
    """

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _handle_response(self, response: httpx.Response) -> Any:
        response.raise_for_status()
        content_type = response.headers.get('content-type', '')
        if 'application/x-ndjson' in content_type:
            import json

            return [json.loads(line) for line in response.text.strip().split('\n') if line]
        return response.json()

    async def _post(self, path: str, data: BaseModel | dict[str, Any]) -> Any:
        payload = data.model_dump(mode='json') if isinstance(data, BaseModel) else data
        response = await self.client.post(path, json=payload)
        return await self._handle_response(response)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.client.get(path, params=params)
        return await self._handle_response(response)

    async def _delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.client.delete(path, params=params)
        return await self._handle_response(response)

    async def _put(self, path: str, data: BaseModel | dict[str, Any]) -> Any:
        payload = data.model_dump(mode='json') if isinstance(data, BaseModel) else data
        response = await self.client.put(path, json=payload)
        return await self._handle_response(response)

    async def _patch(self, path: str, data: BaseModel | dict[str, Any]) -> Any:
        payload = data.model_dump(mode='json') if isinstance(data, BaseModel) else data
        response = await self.client.patch(path, json=payload)
        return await self._handle_response(response)

    # --- Vaults ---
    async def list_vaults(self) -> list[VaultDTO]:
        """List all available vaults."""
        result = await self._get('vaults')
        return [VaultDTO(**v) for v in result]

    async def list_vaults_with_counts(self) -> list[dict[str, Any]]:
        """List all vaults with note counts. Wraps list_vaults for API compat."""
        vaults = await self.list_vaults()
        return [
            {
                'vault': v,
                'note_count': v.note_count,
                'last_note_added_at': v.last_note_added_at,
            }
            for v in vaults
        ]

    async def get_active_vault(self) -> VaultDTO:
        """Get the currently active vault."""
        result = await self._get('vaults', params={'state': 'active'})
        return VaultDTO(**result[0])

    async def get_default_vaults(self) -> DefaultVaultsResponse:
        """Get the active (writer) vault and default reader vaults."""
        result = await self._get('vaults', params={'is_default': True})
        if not result:
            raise Exception('No default vaults found')
        # Parse as DefaultVaultsResponse - first is active, rest are readers
        return DefaultVaultsResponse(
            active_vault=VaultDTO(**result[0]),
            reader_vaults=[VaultDTO(**v) for v in result[1:]],
        )

    async def create_vault(self, request: CreateVaultRequest) -> VaultDTO:
        """Create a new vault."""
        result = await self._post('vaults', request)
        return VaultDTO(**result)

    async def resolve_vault_identifier(self, identifier: str) -> UUID:
        """Resolve a vault name or ID to a UUID."""
        try:
            # Check if it's already a UUID
            return UUID(identifier)
        except ValueError:
            pass

        # Call server to resolve
        result = await self._get(f'vaults/{identifier}')
        return UUID(str(result['id']))

    async def delete_vault(self, vault_id: UUID) -> bool:
        """Delete a vault by ID."""
        try:
            await self._delete(f'vaults/{vault_id}')
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def truncate_vault(self, vault_id: UUID) -> dict[str, int]:
        """Remove all content from a vault without deleting the vault itself."""
        result = await self._post(f'vaults/{vault_id}/truncate', data={})
        return result.get('deleted', {})

    async def set_writer_vault(self, identifier: str) -> dict[str, Any]:
        """Set the active (writer) vault for the current server session."""
        response = await self.client.post(f'vaults/{identifier}/set-writer')
        return await self._handle_response(response)

    async def set_reader_vault(self, identifier: str) -> dict[str, Any]:
        """Set the default reader vault on the server."""
        response = await self.client.post(f'vaults/{identifier}/set-reader')
        return await self._handle_response(response)

    async def get_vault_summary(self, vault_id: UUID) -> VaultSummaryDTO | None:
        """Get the summary for a vault. Returns None if no summary exists."""
        try:
            result = await self._get(f'vaults/{vault_id}/summary')
            return VaultSummaryDTO(**result)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def regenerate_vault_summary(self, vault_id: UUID) -> VaultSummaryDTO:
        """Regenerate the vault summary from all notes."""
        result = await self._post(f'vaults/{vault_id}/summary/regenerate', {})
        return VaultSummaryDTO(**result)

    async def get_session_briefing(
        self,
        vault_id: UUID,
        budget: int = 2000,
        project_id: str | None = None,
    ) -> str:
        """Generate a session briefing for a vault. Returns the briefing markdown."""
        params: dict[str, Any] = {'budget': budget}
        if project_id is not None:
            params['project_id'] = project_id
        result = await self._get(f'vaults/{vault_id}/session-briefing', params=params)
        return result['briefing']

    # --- Memory ---
    async def ingest(
        self, note: NoteCreateDTO, background: bool = False
    ) -> IngestResponse | BatchJobStatus:
        """Ingest a note into Memex."""
        params = {'background': 'true'} if background else None
        response = await self.client.post(
            'ingestions', json=note.model_dump(mode='json'), params=params
        )
        response.raise_for_status()
        if response.status_code == 202:
            return BatchJobStatus(**response.json())
        return IngestResponse(**response.json())

    async def ingest_batch(
        self, notes: list[NoteCreateDTO], vault_id: str | UUID | None = None, batch_size: int = 32
    ) -> BatchJobStatus:
        """Ingest a batch of notes. Returns 202 with a job_id for status tracking."""
        request = BatchIngestRequest(notes=notes, vault_id=vault_id, batch_size=batch_size)
        response = await self.client.post('ingestions/batch', json=request.model_dump(mode='json'))
        response.raise_for_status()
        return BatchJobStatus(**response.json())

    async def ingest_url(
        self, request: IngestURLRequest, background: bool = False
    ) -> IngestResponse | dict[str, str]:
        """Ingest content from a URL."""
        params = {'background': 'true'} if background else None
        response = await self.client.post(
            'ingestions/url', json=request.model_dump(mode='json'), params=params
        )
        response.raise_for_status()
        if response.status_code == 202:
            return response.json()
        return IngestResponse(**response.json())

    async def ingest_file(self, request: IngestFileRequest) -> IngestResponse:
        """Ingest content from a file (server-side path)."""
        result = await self._post('ingestions/file', request)
        return IngestResponse(**result)

    async def ingest_upload(
        self,
        files: list[tuple[str, tuple[str, Any, str]]],
        metadata: dict[str, Any] | None = None,
        background: bool = False,
    ) -> IngestResponse | dict[str, str]:
        """
        Ingest content by uploading files from the client.

        Args:
            files: List of (field_name, (filename, file_handle/bytes, content_type))
            metadata: Optional metadata (name, description, tags, etc.)
            background: If True, returns immediately with 202 Accepted.
        """
        data = {}
        if metadata:
            import json

            data['metadata'] = json.dumps(metadata)

        params = {'background': 'true'} if background else None
        response = await self.client.post(
            'ingestions/upload', data=data, files=files, params=params
        )
        response.raise_for_status()
        if response.status_code == 202:
            return response.json()
        return IngestResponse(**response.json())

    async def get_job_status(self, job_id: UUID) -> BatchJobStatus:
        """Retrieve the current status of a batch ingestion job."""
        result = await self._get(f'ingestions/{job_id}')
        return BatchJobStatus(**result)

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        vault_ids: list[UUID | str] | None = None,
        token_budget: int | None = None,
        strategies: list[str] | None = None,
        include_stale: bool = False,
        include_superseded: bool = False,
        after: dt.datetime | None = None,
        before: dt.datetime | None = None,
        tags: list[str] | None = None,
        source_context: str | None = None,
        reference_date: dt.datetime | None = None,
        expand_query: bool = False,
    ) -> list[MemoryUnitDTO]:
        """Search for memories."""
        request = RetrievalRequest(
            query=query,
            limit=limit,
            offset=offset,
            vault_ids=vault_ids,
            token_budget=token_budget,
            strategies=strategies,
            include_stale=include_stale,
            include_superseded=include_superseded,
            after=after,
            before=before,
            tags=tags,
            source_context=source_context,
            reference_date=reference_date,
            expand_query=expand_query,
        )
        result = await self._post('memories/search', request)
        return [MemoryUnitDTO(**r) for r in result]

    async def summarize(self, query: str, texts: list[str]) -> SummaryResponse:
        """Generate an AI summary of search results."""
        request = SummaryRequest(query=query, texts=texts)
        result = await self._post('memories/summary', request)
        return SummaryResponse(**result)

    async def list_notes(
        self,
        limit: int = 100,
        offset: int = 0,
        vault_id: UUID | None = None,
        vault_ids: list[str | UUID] | None = None,
        after: dt.datetime | None = None,
        before: dt.datetime | None = None,
        template: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> list[NoteListItemDTO]:
        """List all notes."""
        params: dict[str, Any] = {'limit': limit, 'offset': offset}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        if after is not None:
            params['after'] = after.isoformat()
        if before is not None:
            params['before'] = before.isoformat()
        if template is not None:
            params['template'] = template
        if tags:
            params['tags'] = tags
        if status is not None:
            params['status'] = status
        result = await self._get('notes', params=params)
        return [NoteListItemDTO(**d) for d in result]

    async def search_notes(
        self,
        query: str,
        limit: int = 10,
        vault_ids: list[UUID | str] | None = None,
        expand_query: bool = False,
        fusion_strategy: str = 'rrf',
        strategies: list[str] | None = None,
        strategy_weights: dict[str, float] | None = None,
        reason: bool = False,
        summarize: bool = False,
        after: dt.datetime | None = None,
        before: dt.datetime | None = None,
        tags: list[str] | None = None,
        reference_date: dt.datetime | None = None,
    ) -> list[NoteSearchResult]:
        """Search for notes."""
        kwargs: dict[str, Any] = {}
        if strategies is not None:
            kwargs['strategies'] = strategies
        if strategy_weights is not None:
            kwargs['strategy_weights'] = strategy_weights
        if after is not None:
            kwargs['after'] = after
        if before is not None:
            kwargs['before'] = before
        if tags is not None:
            kwargs['tags'] = tags
        if reference_date is not None:
            kwargs['reference_date'] = reference_date
        request = NoteSearchRequest(
            query=query,
            limit=limit,
            vault_ids=vault_ids,
            expand_query=expand_query,
            fusion_strategy=fusion_strategy,
            reason=reason,
            summarize=summarize,
            **kwargs,
        )
        result = await self._post('notes/search', request)
        return [NoteSearchResult(**r) for r in result]

    async def survey(
        self,
        query: str,
        vault_ids: list[UUID | str] | None = None,
        limit_per_query: int = 10,
        token_budget: int | None = None,
    ) -> SurveyResponse:
        """Broad topic survey — decompose, parallel search, grouped results."""
        request = SurveyRequest(
            query=query,
            vault_ids=vault_ids,
            limit_per_query=limit_per_query,
            token_budget=token_budget,
        )
        result = await self._post('survey', request)
        return SurveyResponse(**result)

    async def get_note(self, note_id: UUID) -> NoteDTO:
        """Get a note by ID."""
        result = await self._get(f'notes/{note_id}')
        return NoteDTO(**result)

    async def get_note_metadata(self, note_id: UUID) -> dict[str, Any] | None:
        """Get just the metadata from a note's page index."""
        result = await self._get(f'notes/{note_id}/metadata')
        return result.get('metadata')

    async def get_note_page_index(self, note_id: UUID) -> dict[str, Any] | None:
        """Get the page index (slim tree) for a note."""
        result = await self._get(f'notes/{note_id}/page-index')
        return result.get('page_index')

    async def get_node(self, node_id: UUID) -> NodeDTO | None:
        """Get a specific note node by its ID."""
        try:
            data = await self._get(f'nodes/{node_id}')
            return NodeDTO(**data) if data else None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_nodes(self, node_ids: list[UUID]) -> list[NodeDTO]:
        """Get multiple note nodes by ID."""
        result = await self._post('nodes/batch', {'node_ids': [str(n) for n in node_ids]})
        return [NodeDTO(**d) for d in result]

    async def get_notes_metadata(self, note_ids: list[UUID]) -> list[dict[str, Any]]:
        """Get metadata for multiple notes."""
        return await self._post('notes/metadata/batch', {'note_ids': [str(n) for n in note_ids]})

    async def get_related_notes(self, note_ids: list[UUID]) -> dict[UUID, list[Any]]:
        """Get notes related to the given notes via shared entities."""
        from memex_common.schemas import RelatedNoteDTO

        resp = await self._post('notes/related', {'note_ids': [str(n) for n in note_ids]})
        result: dict[UUID, list[Any]] = {}
        for k, vs in resp.items():
            result[UUID(k)] = [RelatedNoteDTO(**v) for v in vs]
        return result

    async def update_user_notes(self, note_id: UUID, user_notes: str | None) -> dict[str, Any]:
        """Update user notes on an existing note and reprocess into memory graph."""
        return await self._patch(f'notes/{note_id}/user-notes', {'user_notes': user_notes})

    async def delete_note(self, note_id: UUID) -> bool:
        """Delete a note and all associated data."""
        try:
            await self._delete(f'notes/{note_id}')
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def migrate_note(self, note_id: UUID, target_vault_id: UUID | str) -> dict[str, Any]:
        """Move a note to a different vault."""
        return await self._post(
            f'notes/{note_id}/migrate',
            {'target_vault_id': str(target_vault_id)},
        )

    async def delete_entity(self, entity_id: UUID) -> bool:
        """Delete an entity and all associated data."""
        try:
            await self._delete(f'entities/{entity_id}')
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def delete_mental_model(self, entity_id: UUID, vault_id: UUID | None = None) -> bool:
        """Delete a mental model for a specific entity in a specific vault."""
        params = {}
        if vault_id:
            params['vault_id'] = str(vault_id)
        try:
            await self._delete(f'entities/{entity_id}/mental-model', params=params or None)
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    # --- Stats & Overview ---
    async def get_stats_counts(
        self,
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
    ) -> SystemStatsCountsDTO:
        """Get total counts for notes, entities, and reflection queue."""
        params: dict[str, Any] = {}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        result = await self._get('stats/counts', params=params or None)
        return SystemStatsCountsDTO(**result)

    async def get_recent_notes(
        self,
        limit: int = 5,
        vault_id: UUID | None = None,
        vault_ids: list[str | UUID] | None = None,
        after: dt.datetime | None = None,
        before: dt.datetime | None = None,
        template: str | None = None,
    ) -> list[NoteListItemDTO]:
        """Get the most recent notes."""
        params: dict[str, Any] = {'limit': limit, 'sort': '-created_at'}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        if after is not None:
            params['after'] = after.isoformat()
        if before is not None:
            params['before'] = before.isoformat()
        if template is not None:
            params['template'] = template
        result = await self._get('notes', params=params)
        return [NoteListItemDTO(**d) for d in result]

    async def search_entities(
        self,
        query: str,
        limit: int = 20,
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
        entity_type: str | None = None,
    ) -> list[EntityDTO]:
        """Search for entities by name."""
        params: dict[str, Any] = {'q': query, 'limit': limit}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        if entity_type:
            params['entity_type'] = entity_type
        result = await self._get('entities', params=params)
        if not isinstance(result, list):
            result = [result]
        return [EntityDTO(**e) for e in result]

    async def list_entities_ranked(
        self,
        limit: int = 100,
        q: str | None = None,
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
        entity_type: str | None = None,
    ) -> AsyncGenerator[EntityDTO, None]:
        """Stream entities ranked by hybrid score."""
        params: dict[str, Any] = {'limit': limit}
        if q:
            params['q'] = q
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        if entity_type:
            params['entity_type'] = entity_type

        async with self.client.stream('GET', 'entities', params=params) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    import json

                    yield EntityDTO(**json.loads(line))

    async def get_entity(self, entity_id: UUID | str) -> EntityDTO:
        """Get entity details."""
        result = await self._get(f'entities/{entity_id}')
        return EntityDTO(**result)

    async def get_entities(self, entity_ids: list[UUID]) -> list[EntityDTO]:
        """Get multiple entities by ID."""
        result = await self._post('entities/batch', {'entity_ids': [str(e) for e in entity_ids]})
        return [EntityDTO(**e) for e in result]

    async def get_entity_mentions(
        self,
        entity_id: UUID | str,
        limit: int = 20,
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get mentions for an entity."""
        # Returns list of dicts with 'unit': MemoryUnitDTO, 'note': NoteDTO keys
        params: dict[str, Any] = {'limit': limit}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        result = await self._get(f'entities/{entity_id}/mentions', params=params)
        # We can optionally parse them into DTOs here if we want strict typing return,
        # but that's what the schema implies for now (no MentionDTO).
        # To be safe and helpful, let's convert the inner dicts to DTOs
        parsed = []
        for r in result:
            item = {}
            if 'unit' in r:
                item['unit'] = MemoryUnitDTO(**r['unit'])
            if 'note' in r:
                item['note'] = NoteDTO(**r['note'])
            parsed.append(item)
        return parsed

    async def get_bulk_cooccurrences(
        self,
        ids: list[UUID],
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get co-occurrences for a set of entity IDs."""
        ids_str = ','.join(str(i) for i in ids)
        params: dict[str, Any] = {'ids': ids_str}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        return await self._get('cooccurrences', params=params)

    async def get_entity_cooccurrences(
        self,
        entity_id: UUID | str,
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get co-occurrence edges for an entity."""
        params: dict[str, Any] = {'limit': limit}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        return await self._get(f'entities/{entity_id}/cooccurrences', params=params)

    async def get_memory_unit(self, unit_id: UUID | str) -> MemoryUnitDTO:
        """Get memory unit details."""
        result = await self._get(f'memories/{unit_id}')
        return MemoryUnitDTO(**result)

    async def delete_memory_unit(self, unit_id: UUID) -> bool:
        """Delete a memory unit and all associated data."""
        try:
            await self._delete(f'memories/{unit_id}')
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def get_memory_links(
        self,
        unit_id: UUID,
        link_type: str | None = None,
        limit: int = 20,
    ) -> list[MemoryLinkDTO]:
        """Get typed relationship links for a memory unit."""
        params: dict[str, Any] = {'limit': limit}
        if link_type:
            params['link_type'] = link_type
        result = await self._get(f'memories/{unit_id}/links', params=params)
        return [MemoryLinkDTO(**lnk) for lnk in result]

    async def get_note_links(
        self,
        note_id: UUID,
        link_type: str | None = None,
        limit: int = 20,
    ) -> list[MemoryLinkDTO]:
        """Get typed relationship links for a note."""
        params: dict[str, Any] = {'limit': limit}
        if link_type:
            params['link_type'] = link_type
        result = await self._get(f'notes/{note_id}/links', params=params)
        return [MemoryLinkDTO(**lnk) for lnk in result]

    # --- Reflection ---
    async def reflect(self, request: ReflectionRequest) -> ReflectionResultDTO:
        """Trigger reflection on an entity."""
        result = await self._post('reflections', request)
        return ReflectionResultDTO(**result)

    async def reflect_batch(self, requests: list[ReflectionRequest]) -> list[ReflectionResultDTO]:
        """Trigger reflection on a batch of entities."""
        result = await self._post(
            'reflections/batch',
            {'requests': [r.model_dump(mode='json') for r in requests]},
        )
        return [ReflectionResultDTO(**r) for r in result]

    async def get_reflection_queue_batch(self, limit: int = 10) -> list[ReflectionQueueDTO]:
        """Fetch items from the reflection queue."""
        result = await self._get('reflections', params={'limit': limit, 'status': 'queued'})
        return [ReflectionQueueDTO(**u) for u in result]

    async def claim_reflection_queue_batch(self, limit: int = 10) -> list[ReflectionQueueDTO]:
        """Claim reflection queue items for processing."""
        response = await self.client.post('reflections/claim', params={'limit': limit})
        result = await self._handle_response(response)
        return [ReflectionQueueDTO(**u) for u in result]

    async def get_dead_letter_items(
        self,
        limit: int = 50,
        offset: int = 0,
        vault_id: UUID | None = None,
    ) -> list[DeadLetterItemDTO]:
        """List dead-lettered reflection tasks."""
        params: dict[str, Any] = {'limit': limit, 'offset': offset}
        if vault_id:
            params['vault_id'] = str(vault_id)
        result = await self._get('admin/reflection/dlq', params=params)
        return [DeadLetterItemDTO(**item) for item in result]

    async def retry_dead_letter_item(self, item_id: UUID) -> DeadLetterItemDTO:
        """Reset a dead-lettered item back to pending for re-processing."""
        result = await self._post(f'admin/reflection/dlq/{item_id}/retry', {})
        return DeadLetterItemDTO(**result)

    async def get_top_entities(
        self,
        limit: int = 5,
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
        entity_type: str | None = None,
    ) -> list[EntityDTO]:
        """Get top entities by mention count."""
        params: dict[str, Any] = {'limit': limit, 'sort': '-mentions'}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        if entity_type:
            params['entity_type'] = entity_type
        result = await self._get('entities', params=params)
        return [EntityDTO(**e) for e in result]

    async def set_note_status(
        self, note_id: UUID, status: str, linked_note_id: UUID | None = None
    ) -> dict[str, Any]:
        """Set note lifecycle status (active, superseded, appended)."""
        return await self._patch(
            f'notes/{note_id}/status',
            {'status': status, 'linked_note_id': str(linked_note_id) if linked_note_id else None},
        )

    async def update_note_title(self, note_id: UUID, new_title: str) -> dict[str, Any]:
        """Rename a note (updates title in metadata, page index, and doc_metadata)."""
        return await self._patch(f'notes/{note_id}/title', {'new_title': new_title})

    async def update_note_date(self, note_id: UUID, new_date: dt.datetime) -> dict[str, Any]:
        """Update a note's publish_date and cascade delta to memory unit timestamps."""
        return await self._patch(f'notes/{note_id}/date', {'date': new_date.isoformat()})

    async def add_note_assets(self, note_id: UUID, files: dict[str, bytes]) -> dict[str, Any]:
        """Add one or more asset files to an existing note."""
        upload_files = [('files', (filename, content)) for filename, content in files.items()]
        response = await self.client.post(f'notes/{note_id}/assets', files=upload_files)
        response.raise_for_status()
        return response.json()

    async def delete_note_assets(self, note_id: UUID, asset_paths: list[str]) -> dict[str, Any]:
        """Delete one or more asset files from an existing note."""
        response = await self.client.request(
            'DELETE', f'notes/{note_id}/assets', json={'asset_paths': asset_paths}
        )
        response.raise_for_status()
        return response.json()

    async def get_resource(self, path: str) -> bytes:
        """
        Retrieve a raw resource (file) from the server.

        Args:
            path: The path to resource in the filestore.
        """
        response = await self.client.get(f'resources/{path}')
        response.raise_for_status()
        return response.content

    # --- Lineage ---
    async def get_lineage(
        self,
        entity_type: str,
        entity_id: UUID,
        direction: LineageDirection = LineageDirection.UPSTREAM,
        depth: int = 3,
        limit: int = 10,
    ) -> LineageResponse:
        """Retrieve lineage of any entity type via ``/lineage/{entity_type}/{id}``."""
        params = {
            'direction': direction.value,
            'depth': depth,
            'limit': limit,
        }
        result = await self._get(f'lineage/{entity_type}/{entity_id}', params=params)
        return LineageResponse(**result)

    async def get_entity_lineage(
        self,
        entity_id: UUID,
        direction: LineageDirection = LineageDirection.UPSTREAM,
        depth: int = 3,
        limit: int = 10,
    ) -> LineageResponse:
        """Retrieve lineage of an entity.

        .. deprecated:: Use :meth:`get_lineage` instead.
        """
        params = {
            'direction': direction.value,
            'depth': depth,
            'limit': limit,
        }
        result = await self._get(f'entities/{entity_id}/lineage', params=params)
        return LineageResponse(**result)

    async def get_note_lineage(
        self,
        note_id: UUID,
        direction: LineageDirection = LineageDirection.UPSTREAM,
        depth: int = 3,
        limit: int = 10,
    ) -> LineageResponse:
        """Retrieve lineage of a note.

        .. deprecated:: Use :meth:`get_lineage` instead.
        """
        params = {
            'direction': direction.value,
            'depth': depth,
            'limit': limit,
        }
        result = await self._get(f'notes/{note_id}/lineage', params=params)
        return LineageResponse(**result)

    # --- Notes: title search ---
    async def find_notes_by_title(
        self,
        query: str,
        vault_ids: list[UUID | str] | None = None,
        limit: int = 5,
    ) -> list[FindNoteResult]:
        """Fuzzy-search notes by title using trigram similarity."""
        params: dict[str, Any] = {'query': query, 'limit': limit}
        if vault_ids:
            params['vault_id'] = [str(v) for v in vault_ids]
        result = await self._get('notes/find', params=params)
        return [FindNoteResult(**r) for r in result]

    # --- Embeddings ---

    async def embed_text(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text via the REST API."""
        result = await self._post('embed', {'text': text})
        return result['embedding']

    # --- KV store ---
    async def kv_put(
        self,
        value: str,
        key: str,
        embedding: list[float] | None = None,
        ttl_seconds: int | None = None,
    ) -> KVEntryDTO:
        """Create or update a key-value entry."""
        request = KVPutRequest(
            key=key,
            value=value,
            embedding=embedding,
            ttl_seconds=ttl_seconds,
        )
        result = await self._put('kv', request)
        return KVEntryDTO(**result)

    async def kv_get(
        self,
        key: str,
    ) -> KVEntryDTO | None:
        """Get a KV entry by exact key. Returns None if not found."""
        params: dict[str, Any] = {'key': key}
        try:
            result = await self._get('kv/get', params=params)
            return KVEntryDTO(**result)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def kv_search(
        self,
        query: str,
        namespaces: list[str] | None = None,
        limit: int = 5,
    ) -> list[KVEntryDTO]:
        """Semantic search over KV entries."""
        request = KVSearchRequest(
            query=query,
            namespaces=namespaces,
            limit=limit,
        )
        result = await self._post('kv/search', request)
        return [KVEntryDTO(**r) for r in result]

    async def kv_delete(
        self,
        key: str,
    ) -> bool:
        """Delete a KV entry by key."""
        params: dict[str, Any] = {'key': key}
        try:
            await self._delete('kv/delete', params=params)
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def kv_list(
        self,
        namespaces: list[str] | None = None,
        pattern: str | None = None,
    ) -> list[KVEntryDTO]:
        """List KV entries, optionally filtered by namespace prefixes."""
        params: dict[str, Any] = {}
        if namespaces is not None:
            params['namespaces'] = ','.join(namespaces)
        if pattern is not None:
            params['pattern'] = pattern
        result = await self._get('kv', params=params or None)
        return [KVEntryDTO(**r) for r in result]
