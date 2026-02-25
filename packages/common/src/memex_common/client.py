"""
Remote client for the Memex API.
Used by the CLI to interact with a running Memex server.
"""

import logging
from typing import Any, AsyncGenerator
from uuid import UUID

import httpx
from pydantic import BaseModel

from memex_common.schemas import (
    RetrievalRequest,
    ReflectionRequest,
    IngestURLRequest,
    IngestFileRequest,
    AdjustBeliefRequest,
    CreateVaultRequest,
    DefaultVaultsResponse,
    NoteDTO,
    ReflectionResultDTO,
    MemoryUnitDTO,
    VaultDTO,
    ReflectionQueueDTO,
    IngestResponse,
    EntityDTO,
    LineageResponse,
    LineageDirection,
    SystemStatsCountsDTO,
    TokenUsageResponse,
    DocumentDTO,
    DocumentSearchResult,
    DocumentSearchRequest,
    NodeDTO,
    SummaryRequest,
    SummaryResponse,
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

    async def _patch(self, path: str, data: BaseModel | dict[str, Any]) -> Any:
        payload = data.model_dump(mode='json') if isinstance(data, BaseModel) else data
        response = await self.client.patch(path, json=payload)
        return await self._handle_response(response)

    # --- Vaults ---
    async def list_vaults(self) -> list[VaultDTO]:
        """List all available vaults."""
        result = await self._get('vaults')
        return [VaultDTO(**v) for v in result]

    async def get_active_vault(self) -> VaultDTO:
        """Get the currently active vault."""
        result = await self._get('vaults', params={'state': 'active'})
        return VaultDTO(**result[0])

    async def get_default_vaults(self) -> DefaultVaultsResponse:
        """Get the active (writer) vault and attached read-only vaults."""
        result = await self._get('vaults', params={'is_default': True})
        if not result:
            raise Exception('No default vaults found')
        # Parse as DefaultVaultsResponse - first is active, rest are attached
        return DefaultVaultsResponse(
            active_vault=VaultDTO(**result[0]),
            attached_vaults=[VaultDTO(**v) for v in result[1:]],
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

    # --- Memory ---
    async def ingest(self, note: NoteDTO) -> IngestResponse:
        """Ingest a note into Memex."""
        result = await self._post('ingestions', note)
        return IngestResponse(**result)

    async def ingest_url(self, request: IngestURLRequest) -> IngestResponse:
        """Ingest content from a URL."""
        result = await self._post('ingestions/url', request)
        return IngestResponse(**result)

    async def ingest_file(self, request: IngestFileRequest) -> IngestResponse:
        """Ingest content from a file (server-side path)."""
        result = await self._post('ingestions/file', request)
        return IngestResponse(**result)

    async def ingest_upload(
        self,
        files: list[tuple[str, tuple[str, Any, str]]],
        metadata: dict[str, Any] | None = None,
    ) -> IngestResponse:
        """
        Ingest content by uploading files from the client.

        Args:
            files: List of (field_name, (filename, file_handle/bytes, content_type))
            metadata: Optional metadata (name, description, tags, etc.)
        """
        data = {}
        if metadata:
            import json

            data['metadata'] = json.dumps(metadata)

        response = await self.client.post('ingestions/upload', data=data, files=files)
        response.raise_for_status()
        return IngestResponse(**response.json())

    async def search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        vault_ids: list[UUID | str] | None = None,
        skip_opinion_formation: bool = False,
        token_budget: int | None = None,
        strategies: list[str] | None = None,
    ) -> list[MemoryUnitDTO]:
        """Search for memories."""
        request = RetrievalRequest(
            query=query,
            limit=limit,
            offset=offset,
            vault_ids=vault_ids,
            skip_opinion_formation=skip_opinion_formation,
            token_budget=token_budget,
            strategies=strategies,
        )
        result = await self._post('memories/search', request)
        return [MemoryUnitDTO(**r) for r in result]

    async def summarize(self, query: str, texts: list[str]) -> SummaryResponse:
        """Generate an AI summary of search results."""
        request = SummaryRequest(query=query, texts=texts)
        result = await self._post('memories/summary', request)
        return SummaryResponse(**result)

    async def list_notes(self, limit: int = 100, offset: int = 0) -> list[DocumentDTO]:
        """List all notes."""
        result = await self._get('notes', params={'limit': limit, 'offset': offset})
        return [DocumentDTO(**d) for d in result]

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
    ) -> list[DocumentSearchResult]:
        """Search for notes."""
        kwargs: dict[str, Any] = {}
        if strategies is not None:
            kwargs['strategies'] = strategies
        if strategy_weights is not None:
            kwargs['strategy_weights'] = strategy_weights
        request = DocumentSearchRequest(
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
        return [DocumentSearchResult(**r) for r in result]

    async def get_note(self, note_id: UUID) -> DocumentDTO:
        """Get a note by ID."""
        result = await self._get(f'notes/{note_id}')
        return DocumentDTO(**result)

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

    async def delete_note(self, note_id: UUID) -> bool:
        """Delete a note and all associated data."""
        try:
            await self._delete(f'notes/{note_id}')
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

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
    async def get_stats_counts(self) -> SystemStatsCountsDTO:
        """Get total counts for notes, entities, and reflection queue."""
        result = await self._get('stats/counts')
        return SystemStatsCountsDTO(**result)

    async def get_token_usage(self) -> TokenUsageResponse:
        """Get daily aggregated token usage."""
        result = await self._get('stats/token-usage')
        return TokenUsageResponse(**result)

    async def get_recent_notes(self, limit: int = 5) -> list[DocumentDTO]:
        """Get the most recent notes."""
        result = await self._get('notes', params={'limit': limit, 'sort': '-created_at'})
        return [DocumentDTO(**d) for d in result]

    async def search_entities(self, query: str, limit: int = 20) -> list[EntityDTO]:
        """Search for entities by name."""
        response = await self.client.get('entities', params={'q': query, 'limit': limit})
        response.raise_for_status()

        try:
            result = response.json()
        except Exception:
            # Fallback for legacy/stream response (NDJSON)
            # If the server ignores 'q' and returns a stream, we parse lines
            result = []
            import json

            for line in response.text.splitlines():
                if line:
                    try:
                        result.append(json.loads(line))
                    except Exception:
                        pass

        # If result is not a list (e.g. single object or something else), handle it?
        # Expecting list[dict]
        if not isinstance(result, list):
            # Should not happen for this endpoint unless schema changed
            return []

        return [EntityDTO(**e) for e in result]

    async def list_entities_ranked(
        self, limit: int = 100, q: str | None = None
    ) -> AsyncGenerator[EntityDTO, None]:
        """Stream entities ranked by hybrid score."""
        params: dict[str, Any] = {'limit': limit}
        if q:
            params['q'] = q

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

    async def get_entity_mentions(
        self, entity_id: UUID | str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Get mentions for an entity."""
        # Returns list of dicts with 'unit': MemoryUnitDTO, 'document': DocumentDTO keys
        result = await self._get(f'entities/{entity_id}/mentions', params={'limit': limit})
        # We can optionally parse them into DTOs here if we want strict typing return,
        # but that's what the schema implies for now (no MentionDTO).
        # To be safe and helpful, let's convert the inner dicts to DTOs
        parsed = []
        for r in result:
            item = {}
            if 'unit' in r:
                item['unit'] = MemoryUnitDTO(**r['unit'])
            if 'document' in r:
                item['document'] = DocumentDTO(**r['document'])
            parsed.append(item)
        return parsed

    async def get_bulk_cooccurrences(self, ids: list[UUID]) -> list[dict[str, Any]]:
        """Get co-occurrences for a set of entity IDs."""
        ids_str = ','.join(str(i) for i in ids)
        return await self._get('cooccurrences', params={'ids': ids_str})

    async def get_entity_cooccurrences(self, entity_id: UUID | str) -> list[dict[str, Any]]:
        """Get co-occurrence edges for an entity."""
        return await self._get(f'entities/{entity_id}/cooccurrences')

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

    async def get_top_entities(self, limit: int = 5) -> list[EntityDTO]:
        """Get top entities by mention count."""
        result = await self._get('entities', params={'limit': limit, 'sort': '-mentions'})
        return [EntityDTO(**e) for e in result]

    async def adjust_belief(
        self,
        unit_uuid: UUID | str,
        evidence_type_key: str,
        description: str | None = None,
    ) -> None:
        """Adjust belief confidence for a memory unit."""
        request = AdjustBeliefRequest(
            unit_uuid=UUID(str(unit_uuid)),
            evidence_type_key=evidence_type_key,
            description=description,
        )
        await self._patch(f'memories/{unit_uuid}/belief', request)

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
    async def get_entity_lineage(
        self,
        entity_id: UUID,
        direction: LineageDirection = LineageDirection.UPSTREAM,
        depth: int = 3,
        limit: int = 10,
    ) -> LineageResponse:
        """Retrieve lineage of an entity."""
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
        """Retrieve lineage of a note."""
        params = {
            'direction': direction.value,
            'depth': depth,
            'limit': limit,
        }
        result = await self._get(f'notes/{note_id}/lineage', params=params)
        return LineageResponse(**result)
