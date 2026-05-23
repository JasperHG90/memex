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
    IntentClass,
    MemoryLinkDTO,
    NoteAppendRequest,
    NoteAppendResponse,
    NoteCreateDTO,
    ReflectionResultDTO,
    MemoryUnitDTO,
    RiskClass,
    VaultDTO,
    VaultSummaryDTO,
    ReflectionQueueDTO,
    IngestResponse,
    EntityDTO,
    KVEntryDTO,
    KVProcedureEntryDTO,
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
    UnitHistoryNodeDTO,
    SurveyResponse,
)

logger = logging.getLogger('memex.common.client')


class RateLimitExceeded(Exception):
    """Raised when a rate-limited Memex endpoint returns HTTP 429.

    Carries ``retry_after_seconds`` parsed from the server's envelope so
    surface adapters (MCP/Hermes) can present a structured back-off hint.
    """

    def __init__(self, retry_after_seconds: float, message: str) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class ReflectionAbandoned(Exception):
    """Raised when a summarize-node endpoint returns HTTP 503 with
    ``error: 'reflection_abandoned'`` — a concurrent worker refreshed
    the entity's mental model first.

    Carries ``retry_after_seconds`` (mirrors the rate-limit window so a
    naive retry won't immediately 429) and an optional ``hint`` that
    suggests the right next action — typically "re-read the entity's
    mental model directly rather than retrying summarize_node, since
    the fresh state is already persisted by the concurrent worker."
    CAS abandons are benign concurrency contention, NOT task failures.
    """

    def __init__(
        self,
        retry_after_seconds: float,
        message: str,
        hint: str | None = None,
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        self.hint = hint
        super().__init__(message)


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

    async def create_vault(self, name: str, description: str | None = None) -> VaultDTO:
        """Create a new vault.

        Mirrors :pymeth:`memex_core.api.MemexAPI.create_vault`.
        """
        request = CreateVaultRequest(name=name, description=description)
        result = await self._post('vaults', request)
        return VaultDTO(**result)

    async def resolve_vault_identifier(self, identifier: str) -> UUID:
        """Resolve a vault name or ID to a UUID."""
        if not identifier:
            raise ValueError(
                'No vault specified. Pass --vault <name|uuid>, set '
                'vault.active in your config, or set '
                'server.default_active_vault.'
            )
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
        self,
        note: NoteCreateDTO,
        vault_id: UUID | str | None = None,
        event_date: dt.datetime | None = None,
        intent_override: str | None = None,
        risk_override: str | None = None,
        background: bool = False,
    ) -> IngestResponse | BatchJobStatus:
        """Ingest a note into Memex.

        Mirrors :pymeth:`memex_core.api.MemexAPI.ingest`. ``vault_id``,
        ``event_date``, ``intent_override``, and ``risk_override`` override
        the corresponding fields on ``note`` when provided — the merged DTO
        is what's serialized over HTTP. ``background=True`` returns a
        :class:`BatchJobStatus` once the server has queued the work.
        """
        # Build a merged DTO so the wire shape carries every override the
        # caller asked for. We do NOT mutate the caller's DTO in place.
        if (
            vault_id is not None
            or event_date is not None
            or intent_override is not None
            or risk_override is not None
        ):
            overrides: dict[str, Any] = {}
            if vault_id is not None:
                overrides['vault_id'] = vault_id
            if event_date is not None:
                overrides['event_date'] = event_date
            if intent_override is not None:
                overrides['intent_class'] = intent_override
            if risk_override is not None:
                overrides['risk_class'] = risk_override
            note = note.model_copy(update=overrides)

        params = {'background': 'true'} if background else None
        response = await self.client.post(
            'ingestions', json=note.model_dump(mode='json'), params=params
        )
        response.raise_for_status()
        if response.status_code == 202:
            return BatchJobStatus(**response.json())
        return IngestResponse(**response.json())

    async def append_to_note(
        self,
        *,
        note_id: UUID | None = None,
        note_key: str | None = None,
        vault_id: UUID | str | None = None,
        delta: str,
        append_id: UUID,
        joiner: str = 'paragraph',
        user_notes: str | None = None,
        pre_resolved: tuple[UUID, UUID] | None = None,
    ) -> NoteAppendResponse:
        """Atomically append a delta to an existing note (issue #56).

        Mirrors :pymeth:`memex_core.api.MemexAPI.append_to_note`. Identify the
        note by ``note_key`` + ``vault_id`` (preferred) or by ``note_id``.
        ``pre_resolved`` is an in-process optimisation that bypasses the
        server's identifier-resolution pass; it has no HTTP representation —
        passing a non-``None`` value raises ``NotImplementedError`` so the
        gap is visible rather than silently degraded.
        """
        if pre_resolved is not None:
            raise NotImplementedError(
                'pre_resolved is an in-process optimisation; HTTP callers '
                'must let the server resolve the identifier.'
            )
        request = NoteAppendRequest(
            note_id=note_id,
            note_key=note_key,
            vault_id=vault_id,
            delta=delta,
            append_id=append_id,
            joiner=joiner,
            user_notes=user_notes,
        )
        response = await self.client.post('notes/append', json=request.model_dump(mode='json'))
        response.raise_for_status()
        return NoteAppendResponse(**response.json())

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
        include_deprioritized: bool = False,
        debug: bool = False,
        after: dt.datetime | None = None,
        before: dt.datetime | None = None,
        tags: list[str] | None = None,
        source_context: str | None = None,
        reference_date: dt.datetime | None = None,
        expand_query: bool = False,
        intent_class: IntentClass | None = None,
        risk_class: RiskClass | None = None,
        apply_pre_filter: bool = True,
    ) -> list[MemoryUnitDTO]:
        """Search for memories.

        ``intent_class`` and ``risk_class`` are typed as the canonical enums
        (``IntentClass`` / ``RiskClass``). Callers holding a validated string
        (e.g. CLI / MCP / Hermes after their own pre-flight check) should
        construct the enum at the boundary: ``IntentClass(value)``.
        """
        request = RetrievalRequest(
            query=query,
            limit=limit,
            offset=offset,
            vault_ids=vault_ids,
            token_budget=token_budget,
            strategies=strategies,
            include_stale=include_stale,
            include_superseded=include_superseded,
            include_deprioritized=include_deprioritized,
            debug=debug,
            after=after,
            before=before,
            tags=tags,
            source_context=source_context,
            reference_date=reference_date,
            expand_query=expand_query,
            intent_class=intent_class,
            risk_class=risk_class,
            apply_pre_filter=apply_pre_filter,
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
        date_field: str = 'coalesce',
        slim: bool = False,
    ) -> list[NoteListItemDTO]:
        """List all notes.

        ``date_field`` controls which column ``after`` / ``before`` filter on:
        ``'coalesce'`` (default; preserves legacy server behaviour),
        ``'created_at'`` (ingest time), or ``'publish_date'`` (authored date).
        """
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
        params['date_field'] = date_field
        if slim:
            params['slim'] = 'true'
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
        mmr_lambda: float | None = None,
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
        if mmr_lambda is not None:
            kwargs['mmr_lambda'] = mmr_lambda
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
        after: dt.datetime | None = None,
        before: dt.datetime | None = None,
        reference_date: dt.datetime | None = None,
    ) -> SurveyResponse:
        """Broad topic survey — decompose, parallel search, grouped results."""
        request = SurveyRequest(
            query=query,
            vault_ids=vault_ids,
            limit_per_query=limit_per_query,
            token_budget=token_budget,
            after=after,
            before=before,
            reference_date=reference_date,
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

    async def scan_entity_merges(
        self,
        *,
        top_n: int | None = None,
        scan_cooldown_days: int | None = None,
        pair_threshold: float | None = None,
        cluster_min_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Run a one-shot cross-batch entity-cluster collapse scan.

        Emits one MaintenanceProposal per surviving cluster (cohesion-guarded);
        on rescan, existing pending findings are UPDATEd in place. Does NOT
        apply the collapse — operators approve via ``memex lint resolve``.
        """
        params: dict[str, Any] = {}
        if top_n is not None:
            params['top_n'] = top_n
        if scan_cooldown_days is not None:
            params['scan_cooldown_days'] = scan_cooldown_days
        if pair_threshold is not None:
            params['pair_threshold'] = pair_threshold
        if cluster_min_threshold is not None:
            params['cluster_min_threshold'] = cluster_min_threshold
        response = await self.client.post('entities/scan-merges', params=params or None, json={})
        return await self._handle_response(response)

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

    # --- Diagnostics ---
    async def get_diagnostics_summary(self, vault_id: UUID | str) -> dict[str, Any]:
        """Fetch the diagnostics summary for a vault."""
        return await self._get(f'diagnostics/summary/{vault_id}')

    async def get_diagnostics_lint(self, vault_id: UUID | str) -> dict[str, Any]:
        """Fetch the lint dashboard pivot for a vault.

        Returns ``{vault_id, counts_by_type_status_source, pending_by_type,
        top_5_pending}``. Operator/observability view; orthogonal to
        ``/lint/status`` (single count) and ``/lint/findings`` (paginated rows).
        """
        return await self._get(f'diagnostics/lint/{vault_id}')

    async def get_diagnostics_retrieval(
        self, vault_id: UUID | str, top_n: int = 50
    ) -> dict[str, Any]:
        """Fetch the retrieval heatmap (top-N entities by outcome volume)."""
        return await self._get(f'diagnostics/retrieval/{vault_id}', params={'top_n': top_n})

    async def get_diagnostics_manifold(
        self,
        vault_id: UUID | str,
        force_refresh: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        """Fetch the UMAP manifold. Returns (status_code, payload).

        200 → warm cache hit (payload is the manifold).
        202 → cold compute kicked off (payload contains task_id).
        501 → umap-learn not installed.
        """
        params = {'force_refresh': 'true'} if force_refresh else None
        response = await self.client.get(f'diagnostics/manifold/{vault_id}', params=params)
        if response.status_code == 501:
            response.raise_for_status()
        return response.status_code, response.json()

    # --- Consolidation ---
    async def consolidation_tick(
        self,
        vault_id: UUID | str | None = None,
        *,
        dry_run: bool = False,
        budget: int | None = None,
    ) -> dict[str, Any]:
        """Run a consolidation tick. ``vault_id=None`` ticks every vault."""
        body: dict[str, Any] = {'dry_run': dry_run}
        if vault_id is not None:
            body['vault_id'] = str(vault_id)
        if budget is not None:
            body['budget'] = budget
        return await self._post('consolidation/tick', body)

    async def consolidation_status(self, vault_id: UUID | str | None = None) -> dict[str, Any]:
        """Return the most recent consolidation tick row per vault (or for one vault)."""
        params = {'vault_id': str(vault_id)} if vault_id is not None else None
        return await self._get('consolidation/status', params=params)

    # --- Outcomes ---
    async def record_outcome(
        self,
        unit_ids: list[str] | None = None,
        success: bool | None = None,
        vault_id: str | None = None,
        outcome_confidence: float = 1.0,
        reason: str | None = None,
        *,
        units: list[dict[str, Any]] | None = None,
        caller_id: str | None = None,
        turn_outcome: str | None = None,
        retrieved_set_size: int | None = None,
        exploration_tagged: bool = False,
    ) -> dict[str, Any]:
        """Record an outcome over HTTP.

        Preferred shape: ``units=[{unit_id, verb, reason}, ...]``. Legacy
        ``(unit_ids, success)`` shape still accepted (server emits
        FutureWarning on translation).
        """
        body: dict[str, Any] = {
            'outcome_confidence': outcome_confidence,
        }
        if success is not None:
            body['success'] = success
        if units is not None:
            body['units'] = units
        if unit_ids is not None:
            body['unit_ids'] = unit_ids
        if vault_id is not None:
            body['vault_id'] = vault_id
        if reason is not None:
            body['reason'] = reason
        if caller_id is not None:
            body['caller_id'] = caller_id
        if turn_outcome is not None:
            body['turn_outcome'] = turn_outcome
        if retrieved_set_size is not None:
            body['retrieved_set_size'] = retrieved_set_size
        if exploration_tagged:
            body['exploration_tagged'] = True
        return await self._post('outcomes/record', body)

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
        date_field: str = 'coalesce',
        slim: bool = False,
    ) -> list[NoteListItemDTO]:
        """Get the most recent notes. ``date_field`` matches ``list_notes``."""
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
        params['date_field'] = date_field
        if slim:
            params['slim'] = 'true'
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
        params: dict[str, Any] = {'query': query, 'limit': limit}
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
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
        entity_type: str | None = None,
        slim: bool = False,
    ) -> AsyncGenerator[EntityDTO, None]:
        """Stream entities ranked by hybrid score.

        For name-based search, use :pymeth:`search_entities` instead — the
        HTTP `/entities` endpoint overloads both list and search, but
        exposing both verbs from one client method invites confusion.
        """
        params: dict[str, Any] = {'limit': limit}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        if entity_type:
            params['entity_type'] = entity_type
        if slim:
            params['slim'] = 'true'

        async with self.client.stream('GET', 'entities', params=params) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    import json

                    yield EntityDTO(**json.loads(line))

    async def get_entity(
        self,
        entity_id: UUID | str,
        vault_id: UUID | str | None = None,
    ) -> EntityDTO:
        """Get entity details."""
        params: dict[str, Any] = {}
        if vault_id is not None:
            params['vault_id'] = str(vault_id)
        result = await self._get(f'entities/{entity_id}', params=params or None)
        return EntityDTO(**result)

    async def get_entities(
        self,
        entity_ids: list[UUID],
        vault_id: UUID | str | None = None,
    ) -> list[EntityDTO]:
        """Get multiple entities by ID."""
        params: dict[str, Any] = {}
        if vault_id is not None:
            params['vault_id'] = str(vault_id)
        response = await self.client.post(
            'entities/batch',
            json={'entity_ids': [str(e) for e in entity_ids]},
            params=params or None,
        )
        response.raise_for_status()
        return [EntityDTO(**e) for e in response.json()]

    async def get_entity_mentions(
        self,
        entity_id: UUID | str,
        limit: int = 20,
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
        include_stale: bool = False,
        include_superseded: bool = False,
        include_deprioritized: bool = False,
    ) -> list[dict[str, Any]]:
        """Get mentions for an entity."""
        # Returns list of dicts with 'unit': MemoryUnitDTO, 'note': NoteDTO keys
        params: dict[str, Any] = {'limit': limit}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        if include_stale:
            params['include_stale'] = 'true'
        if include_superseded:
            params['include_superseded'] = 'true'
        if include_deprioritized:
            params['include_deprioritized'] = 'true'
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
        entity_ids: list[UUID],
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
    ) -> list[dict[str, Any]]:
        """Get co-occurrences for a set of entity IDs."""
        ids_str = ','.join(str(i) for i in entity_ids)
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

    async def get_memory_units_by_chunks(
        self,
        chunk_ids: list[UUID | str],
        vault_id: UUID | str,
    ) -> list[MemoryUnitDTO]:
        """Fetch memory units belonging to the named chunks (vault-scoped).

        Mirrors the service-layer short-circuit (see
        ``memex_core.services.stats.StatsService.get_memory_units_by_chunks``)
        so an empty input list never costs a network round-trip.
        """
        if not chunk_ids:
            return []
        body = {
            'chunk_ids': [str(c) for c in chunk_ids],
            'vault_id': str(vault_id),
        }
        result = await self._post('memories/by-chunks', body)
        return [MemoryUnitDTO(**r) for r in result]

    async def list_memory_units_by_note(
        self,
        note_id: UUID | str,
        vault_id: UUID | str,
    ) -> list[MemoryUnitDTO]:
        """Fetch memory units belonging to a note (vault-scoped).

        Used by the eval suite to resolve note_keys to unit IDs after
        ingestion. The server enforces vault-scoping; passing a vault_id
        the caller's API key is not authorized for returns 403.
        """
        result = await self._get(
            f'notes/{note_id}/memory_units', params={'vault_id': str(vault_id)}
        )
        return [MemoryUnitDTO(**r) for r in result]

    async def get_system_config(self) -> dict[str, Any]:
        """Fetch the resolved server config with secrets redacted.

        Admin-only — the server requires an admin API key. The returned
        shape mirrors ``MemexConfig.model_dump(mode='json')`` with secret
        leaves replaced by ``'<redacted>'`` and sibling ``<key>_set``
        booleans added.
        """
        return await self._get('system/config')

    async def delete_memory_unit(self, unit_id: UUID) -> bool:
        """Delete a memory unit and all associated data."""
        try:
            await self._delete(f'memories/{unit_id}')
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False
            raise

    async def deprioritize_memory_unit(
        self,
        unit_id: UUID,
        reason: str,
        *,
        vault_id: UUID | str | None = None,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> MemoryUnitDTO:
        """Deprioritize a memory unit (non-destructive).

        ``vault_id`` is REQUIRED by the server (vault-scoping invariant); kept
        optional here only so legacy callers get a clear server-side 422.
        ``actor`` is forwarded to the audit log; the server may override
        from authenticated context. ``background_tasks`` is accepted for
        signature parity with the in-process API but has no HTTP form — a
        non-``None`` value raises ``NotImplementedError``.
        """
        if background_tasks is not None:
            raise NotImplementedError(
                'background_tasks is a FastAPI server-internal handle; HTTP '
                'clients cannot pass one. The server runs its own background '
                'queue post-write.'
            )
        body: dict[str, Any] = {'reason': reason}
        if vault_id is not None:
            body['vault_id'] = str(vault_id)
        if actor is not None:
            body['actor'] = actor
        result = await self._post(f'memories/{unit_id}/deprioritize', body)
        return MemoryUnitDTO(**result)

    async def restore_memory_unit(
        self,
        unit_id: UUID,
        *,
        vault_id: UUID | str | None = None,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> MemoryUnitDTO:
        """Restore a previously-deprioritized memory unit.

        ``vault_id`` is REQUIRED by the server (vault-scoping invariant); kept
        optional here only so legacy callers get a clear server-side 422.
        ``actor`` is forwarded to the audit log; the server may override
        from authenticated context. ``background_tasks`` is accepted for
        signature parity but cannot cross the HTTP boundary.
        """
        if background_tasks is not None:
            raise NotImplementedError(
                'background_tasks is a FastAPI server-internal handle; HTTP '
                'clients cannot pass one.'
            )
        body: dict[str, Any] = {}
        if vault_id is not None:
            body['vault_id'] = str(vault_id)
        if actor is not None:
            body['actor'] = actor
        result = await self._post(f'memories/{unit_id}/restore', body)
        return MemoryUnitDTO(**result)

    async def reconsolidate_entity(
        self,
        entity_id: UUID,
        vault_id: UUID,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Re-evaluate memories for an entity under a per-entity lock."""
        return await self._post(
            'memory/reconsolidate',
            {
                'entity_id': str(entity_id),
                'vault_id': str(vault_id),
                'timeout_seconds': timeout_seconds,
            },
        )

    async def consolidate_vault(
        self,
        vault_id: UUID,
        *,
        dry_run: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Vault-wide low-Memory Worth unit consolidation.

        ``actor`` is forwarded to the audit log; the server may override
        from authenticated context. On HTTP 429 (per-vault rate limit),
        raises a structured ``RateLimitExceeded`` carrying
        ``retry_after_seconds`` so callers can surface the back-off time
        without re-parsing the body. Mirrors :meth:`summarize_node`.
        """
        body: dict[str, Any] = {'vault_id': str(vault_id), 'dry_run': dry_run}
        if actor is not None:
            body['actor'] = actor
        response = await self.client.post('memory/consolidate', json=body)
        if response.status_code == 429:
            payload = response.json()
            raise RateLimitExceeded(
                retry_after_seconds=float(payload.get('retry_after_seconds', 0.0)),
                message=str(payload.get('message', 'Rate limit exceeded.')),
            )
        response.raise_for_status()
        return response.json()

    async def get_memory_links(
        self,
        unit_ids: list[UUID],
        link_types: list[str] | None = None,
        limit: int = 20,
    ) -> dict[UUID, list[MemoryLinkDTO]]:
        """Get typed relationship links for memory units, batched.

        Matches :pymeth:`memex_core.api.MemexAPI.get_memory_links`. The HTTP
        endpoint is per-unit (`/memories/{id}/links`); this fans out one
        request per unit and aggregates into the batch shape expected by MCP
        / CLI / Hermes callers.
        """
        out: dict[UUID, list[MemoryLinkDTO]] = {}
        # The HTTP route accepts a single `link_type` query param. When the
        # caller asks for multiple types, fan out per type so the union surfaces.
        link_type_iter: list[str | None] = list(link_types) if link_types else [None]
        for unit_id in unit_ids:
            collected: list[MemoryLinkDTO] = []
            for lt in link_type_iter:
                params: dict[str, Any] = {'limit': limit}
                if lt:
                    params['link_type'] = lt
                result = await self._get(f'memories/{unit_id}/links', params=params)
                collected.extend(MemoryLinkDTO(**lnk) for lnk in result)
            out[unit_id] = collected
        return out

    async def get_unit_history(
        self,
        unit_id: UUID,
        *,
        vault_id: UUID,
        max_depth: int = 10,
    ) -> UnitHistoryNodeDTO:
        """Walk the contradiction graph backward from ``unit_id``.

        Returns a ``UnitHistoryNodeDTO`` tree (root + nested predecessors).
        """
        params: dict[str, Any] = {
            'vault_id': str(vault_id),
            'max_depth': max_depth,
        }
        result = await self._get(f'memories/{unit_id}/history', params=params)
        return UnitHistoryNodeDTO(**result)

    async def get_note_links(
        self,
        note_ids: list[UUID],
        link_types: list[str] | None = None,
        limit: int = 20,
    ) -> dict[UUID, list[MemoryLinkDTO]]:
        """Get typed relationship links for notes, batched.

        Matches :pymeth:`memex_core.api.MemexAPI.get_note_links`. The HTTP
        endpoint is per-note (`/notes/{id}/links`); this fans out one request
        per (note, link_type) pair and aggregates into the batch shape
        expected by MCP / CLI / Hermes callers.
        """
        out: dict[UUID, list[MemoryLinkDTO]] = {}
        link_type_iter: list[str | None] = list(link_types) if link_types else [None]
        for note_id in note_ids:
            collected: list[MemoryLinkDTO] = []
            for lt in link_type_iter:
                params: dict[str, Any] = {'limit': limit}
                if lt:
                    params['link_type'] = lt
                result = await self._get(f'notes/{note_id}/links', params=params)
                collected.extend(MemoryLinkDTO(**lnk) for lnk in result)
            out[note_id] = collected
        return out

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

    async def summarize_node(
        self,
        entity_id: UUID,
        *,
        scope: str = 'incremental',
        vault_id: UUID | None = None,
    ) -> ReflectionResultDTO:
        """Synchronous on-demand reflection (rate-limited per (entity, vault)).

        On HTTP 429, raises a structured ``RateLimitExceeded`` carrying
        ``retry_after_seconds`` so callers can surface the back-off time
        without re-parsing the body. On HTTP 503 with
        ``error='reflection_abandoned'``, raises ``ReflectionAbandoned``
        with the same ``retry_after_seconds`` shape — CAS abandons are
        benign concurrency, surface as "try again shortly".
        """
        body: dict[str, Any] = {'entity_id': str(entity_id), 'scope': scope}
        if vault_id is not None:
            body['vault_id'] = str(vault_id)
        response = await self.client.post('memories/summarize-node', json=body)
        if response.status_code == 429:
            payload = response.json()
            raise RateLimitExceeded(
                retry_after_seconds=float(payload.get('retry_after_seconds', 0.0)),
                message=str(payload.get('message', 'Rate limit exceeded.')),
            )
        if response.status_code == 503:
            payload = response.json()
            if payload.get('error') == 'reflection_abandoned':
                hint = payload.get('hint')
                raise ReflectionAbandoned(
                    retry_after_seconds=float(payload.get('retry_after_seconds', 60.0)),
                    message=str(
                        payload.get(
                            'message',
                            'Reflection abandoned by concurrent refresh; retry.',
                        )
                    ),
                    hint=str(hint) if hint else None,
                )
        response.raise_for_status()
        return ReflectionResultDTO(**response.json())

    async def get_reflection_queue_batch(
        self,
        limit: int = 10,
        vault_id: UUID | None = None,
        vault_ids: list[UUID | str] | None = None,
    ) -> list[ReflectionQueueDTO]:
        """Fetch items from the reflection queue."""
        params: dict[str, Any] = {'limit': limit, 'status': 'queued'}
        resolved = resolve_vault_list(vault_id, vault_ids)
        if resolved:
            params['vault_id'] = [str(v) for v in resolved]
        result = await self._get('reflections', params=params)
        return [ReflectionQueueDTO(**u) for u in result]

    async def claim_reflection_queue_batch(
        self,
        limit: int = 10,
        vault_id: UUID | None = None,
    ) -> list[ReflectionQueueDTO]:
        """Claim reflection queue items for processing."""
        params: dict[str, Any] = {'limit': limit}
        if vault_id is not None:
            params['vault_id'] = str(vault_id)
        response = await self.client.post('reflections/claim', params=params)
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
        """Set note lifecycle status (active or superseded)."""
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
        threshold: float = 0.3,
    ) -> list[FindNoteResult]:
        """Fuzzy-search notes by title using trigram similarity."""
        params: dict[str, Any] = {'query': query, 'limit': limit, 'threshold': threshold}
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
        *,
        include_history: bool = False,
    ) -> KVEntryDTO | KVProcedureEntryDTO | None:
        """Get a KV entry by exact key. Returns None if not found.

        For ``procedure:`` keys, ``include_history=True`` returns a
        :class:`KVProcedureEntryDTO` whose ``value`` field is the
        structured envelope ({value, version, history}).
        """
        params: dict[str, Any] = {'key': key, 'include_history': include_history}
        try:
            result = await self._get('kv/get', params=params)
            if (
                include_history
                and key.startswith('procedure:')
                and isinstance(result.get('value'), dict)
            ):
                return KVProcedureEntryDTO(**result)
            return KVEntryDTO(**result)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def kv_search(
        self,
        query_embedding: list[float],
        namespaces: list[str] | None = None,
        limit: int = 5,
    ) -> list[KVEntryDTO]:
        """Semantic search over KV entries.

        Mirrors :pymeth:`memex_core.api.MemexAPI.kv_search` — takes a
        pre-computed query embedding. To search from text, embed it via
        :pymeth:`embed_text` first, OR use :pymeth:`kv_search_text` for
        the server-side encode path.
        """
        request = KVSearchRequest(
            query_embedding=query_embedding,
            namespaces=namespaces,
            limit=limit,
        )
        result = await self._post('kv/search', request)
        return [KVEntryDTO(**r) for r in result]

    async def kv_search_text(
        self,
        query: str,
        namespaces: list[str] | None = None,
        limit: int = 5,
    ) -> list[KVEntryDTO]:
        """Convenience wrapper: server embeds ``query`` before searching.

        Exists because the HTTP route accepts text-query input and embeds
        server-side; callers without a local embedder can rely on this path.
        :pymeth:`kv_search` requires the embedding to be pre-computed.
        """
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
        limit: int = 100,
        exclude_prefix: str | None = None,
        key_prefix: str | None = None,
        pattern: str | None = None,
    ) -> list[KVEntryDTO]:
        """List KV entries, optionally filtered by namespace prefixes."""
        params: dict[str, Any] = {'limit': limit}
        if namespaces is not None:
            params['namespaces'] = ','.join(namespaces)
        if exclude_prefix is not None:
            params['exclude_prefix'] = exclude_prefix
        if key_prefix is not None:
            params['key_prefix'] = key_prefix
        if pattern is not None:
            params['pattern'] = pattern
        result = await self._get('kv', params=params)
        return [KVEntryDTO(**r) for r in result]

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Maintenance ledger (lint)
    # ------------------------------------------------------------------

    async def lint_status(
        self,
        *,
        scope: str = 'vault',
        vault_id: str | None = None,
    ) -> dict[str, Any]:
        """Pending finding counts. ``scope`` ∈ {'vault', 'global', 'all'}."""
        params: dict[str, Any] = {'scope': scope}
        if vault_id is not None:
            params['vault_id'] = vault_id
        return await self._get('lint/status', params=params)

    async def lint_findings(
        self,
        *,
        vault_id: str | None = None,
        lint_type: str | None = None,
        status: str = 'pending',
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List maintenance findings with optional filters."""
        params: dict[str, Any] = {'status': status, 'limit': limit, 'offset': offset}
        if vault_id is not None:
            params['vault_id'] = vault_id
        if lint_type is not None:
            params['lint_type'] = lint_type
        return await self._get('lint/findings', params=params)

    async def lint_dismiss(
        self,
        finding_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Flip a pending finding to ``dismissed``.

        ``note`` (optional) is stored at ``evidence.resolution.note`` for
        the audit trail. Dismiss is non-destructive; no attended-mode gate.
        """
        body: dict[str, Any] = {}
        if note is not None:
            body['note'] = note
        return await self._post(f'lint/findings/{finding_id}/dismiss', body)

    async def lint_resolve(
        self,
        finding_id: str,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        note: str | None = None,
        legacy_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Flip a pending finding to ``resolved``.

        New cockpit shape — optional ``action`` (action_id), ``params``
        (forwarded to ``action.execute``), and ``note`` (reviewer's
        free-form text stored at ``evidence.resolution.note``). When
        ``action`` is supplied the server runs the canned action via the
        proposal_actions registry, captures ``prior_state`` /
        ``applied_state`` under ``evidence.resolution.followup``, and
        flips status atomically. Gates on attended-mode.

        Legacy shape — for ``entity_collapse_cluster`` findings, the
        server still accepts ``{"winner_id": ...}`` or
        ``{"winner_canonical_name": ...}`` at the top level of the body;
        pass these via ``legacy_params`` to keep them out of the new
        ``params`` slot.
        """
        body: dict[str, Any] = {}
        if action is not None:
            body['action'] = action
        if params is not None:
            body['params'] = params
        if note is not None:
            body['note'] = note
        if legacy_params:
            for key, value in legacy_params.items():
                body[key] = value
        return await self._post(f'lint/findings/{finding_id}/resolve', body)

    async def lint_apply_winner(self, finding_id: str) -> dict[str, Any]:
        """Apply a winner-proposal finding's recorded action.

        Legacy entry point — the finding's ``evidence.action`` literal
        drives the mutation (mark a unit stale, mark a note superseded,
        rewrite a contradicts link as refines). New cockpit code should
        prefer :meth:`lint_resolve` with an ``action`` argument so the
        action registry's reversibility contract applies.
        """
        return await self._post(f'lint/findings/{finding_id}/apply', {})

    async def lint_reverse(self, finding_id: str) -> dict[str, Any]:
        """Reverse a previously applied resolution.

        Server dispatches on ``evidence.resolution.followup.action`` (new
        cockpit shape) when present, falling back to the legacy
        winner-proposal path otherwise. Forward-only actions return 409
        with ``detail.reason='forward_only'`` and no audit row.
        """
        return await self._post(f'lint/findings/{finding_id}/reverse', {})

    # Back-compat alias — old call sites bound to lint_reverse_winner.
    lint_reverse_winner = lint_reverse

    async def run_lint_rules(self, vault_id: str | UUID) -> dict[str, Any]:
        """Synchronously run the V1 lint rule registry for ``vault_id``.

        Mirrors the periodic scheduler's lint task — same entrypoint, same
        idempotent insert. Used by the eval-suite ``lint_run`` setup
        action so lint findings are deterministically present before a
        scenario asserts on them.
        """
        return await self._post(f'lint/run/{vault_id}', {})

    async def run_lint_llm(self, vault_id: str | UUID) -> dict[str, Any]:
        """Synchronously run the LLM-gated lint pass for ``vault_id``.

        Mirrors the periodic scheduler's lint_llm task. Returns 503 when
        ``lint_llm.enabled=False`` or ``cost_cap_per_24h=0``. NLI is eager
        loaded at server startup when ``polarity.enabled=True``; otherwise
        this call lazy-loads NLI on first invocation.
        """
        return await self._post(f'lint/llm/run/{vault_id}', {})

    async def lint_get_flags(
        self,
        *,
        vault_id: str | None = None,
        lint_type: str | None = None,
        target_type: str | None = None,
        status: str = 'pending',
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Agent surface — cursor-paginated, shape-stable findings list.

        Returns ``{findings: [...], next_cursor: str|null}``.
        """
        params: dict[str, Any] = {'status': status, 'limit': limit}
        if vault_id is not None:
            params['vault_id'] = vault_id
        if lint_type is not None:
            params['lint_type'] = lint_type
        if target_type is not None:
            params['target_type'] = target_type
        if cursor is not None:
            params['cursor'] = cursor
        return await self._get('lint/flags', params=params)
