from typing import cast, Self, Any, AsyncGenerator
import asyncio
import hashlib
import pathlib as plb
import logging
from uuid import UUID
from functools import cached_property
from datetime import datetime

from sqlalchemy.exc import IntegrityError

import dspy

from memex_common.exceptions import (
    VaultNotFoundError,
)
from memex_common.schemas import (
    LineageResponse,
    LineageDirection,
    NoteSearchResult,
    NodeDTO,
)
from memex_core.config import MemexConfig, GLOBAL_VAULT_ID
from memex_core.models import NoteMetadata
from memex_core.storage import (
    calculate_deep_hash,
    Manifest,
)
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.templates import MemexTemplateFromFile

# Engines and Models
from memex_core.memory.engine import MemoryEngine, _build_contradiction_engine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.document_search import NoteSearchEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.reflect.models import (
    ReflectionRequest,
    ReflectionResult,
)
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.memory.sql_models import MemoryUnit
from memex_core.memory.models.embedding import FastEmbedder
from memex_core.memory.models.reranking import FastReranker
from memex_core.memory.models.ner import FastNERModel
from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.extraction.core import ExtractSemanticFacts
from memex_core.processing.files import FileContentProcessor
from memex_core.processing.batch import JobManager
from memex_core.services.entities import EntityService
from memex_core.services.ingestion import IngestionService
from memex_core.services.kv import KVService
from memex_core.services.lineage import LineageService
from memex_core.services.notes import NoteService
from memex_core.services.reflection import ReflectionService
from memex_core.services.search import SearchService
from memex_core.services.stats import StatsService
from memex_core.services.vaults import VaultService, _VAULT_RESOLUTION_CACHE

logger = logging.getLogger('memex.core.api')


class NoteInput:
    """
    Represents a Note artifact (markdown content + assets).
    Acts as a DTO for transferring content into Memex.
    """

    def __init__(
        self,
        name: str | None,
        description: str,
        content: bytes,
        files: dict[str, bytes] | None = None,
        tags: list[str] | None = None,
        source_uri: str | None = None,
        original_content_hash: str | None = None,
        note_key: str | None = None,
        user_notes: str | None = None,
    ):
        self._metadata = NoteMetadata(name=name, description=description)
        self._content = content

        if user_notes and user_notes.strip():
            notes_block = f'## User Notes\n\n{user_notes.strip()}\n\n'
            text = self._content.decode('utf-8')
            try:
                closing = text.index('---', 3)
            except ValueError:
                closing = -1
            if text.startswith('---') and closing != -1:
                end = closing + 3
                self._content = (text[:end] + '\n\n' + notes_block + text[end:]).encode('utf-8')
            else:
                self._content = (notes_block + text).encode('utf-8')
        self._files = files or {}
        self.source_uri = source_uri
        self.original_content_hash = original_content_hash
        self._explicit_key = note_key
        # Update metadata fields
        self._metadata.update('files', list(self._files.keys()))
        self._metadata.update('tags', tags or [])
        self._metadata.update('etag', self.etag)
        self._metadata.update('uuid', self.idempotency_key)

    @cached_property
    def etag(self) -> str:
        """Compute the MD5 etag of the template content."""
        return hashlib.md5(self._content).hexdigest()

    @cached_property
    def metadata(self) -> str:
        return self._metadata.model_dump_json()

    @cached_property
    def note_key(self) -> str:
        """Stable identity derived from origin, not content.

        Used for incremental ingestion: the same logical document across edits
        should produce the same note_key.
        """
        if self._explicit_key:
            try:
                # Check if it's already a valid UUID
                UUID(self._explicit_key)
                return self._explicit_key
            except ValueError:
                # If not, hash it to produce a stable UUID
                return hashlib.md5(self._explicit_key.encode('utf-8')).hexdigest()

        if self.source_uri:
            return hashlib.md5(self.source_uri.encode('utf-8')).hexdigest()

        # No stable key — fall back to content-addressed (no incremental benefits)
        return self.content_fingerprint

    @cached_property
    def content_fingerprint(self) -> str:
        """Version fingerprint for idempotency.

        Changes when content changes. Used as Gate 2 in the two-gate check:
        same note_key + same fingerprint = skip (already processed).
        """
        if self.source_uri and self.original_content_hash:
            return hashlib.md5(
                f'{self.source_uri}{self.original_content_hash}'.encode('utf-8')
            ).hexdigest()

        # We exclude date_created from hashing to ensure content-addressable idempotency
        hash_metadata = self._metadata.model_dump_json(exclude={'date_created', 'uuid'})
        return calculate_deep_hash(
            metadata=hash_metadata.encode('utf-8'), content=self._content, aux_files=self._files
        )

    @cached_property
    def idempotency_key(self) -> str:
        """Stable identity key for idempotent ingestion (delegates to note_key).

        This is an MD5 hex digest, not a UUID despite the metadata field name.
        """
        return self.note_key

    @classmethod
    def calculate_idempotency_key_from_dto(cls, dto: Any) -> str:
        """Calculate the idempotency key (note_key) for a NoteCreateDTO."""
        content = dto.content
        files = dto.files
        # DTO might have note_key
        doc_key = getattr(dto, 'note_key', None)
        temp_note = cls(
            name=dto.name,
            description=dto.description,
            content=content,
            files=files,
            tags=dto.tags,
            note_key=doc_key,
            user_notes=getattr(dto, 'user_notes', None),
        )
        return temp_note.idempotency_key

    @classmethod
    def calculate_fingerprint_from_dto(cls, dto: Any) -> str:
        """Calculate the content_fingerprint for a NoteCreateDTO without full instantiation."""
        content = dto.content
        files = dto.files
        # DTO might have note_key
        doc_key = getattr(dto, 'note_key', None)
        temp_note = cls(
            name=dto.name,
            description=dto.description,
            content=content,
            files=files,
            tags=dto.tags,
            note_key=doc_key,
            user_notes=getattr(dto, 'user_notes', None),
        )
        return temp_note.content_fingerprint

    @cached_property
    def manifest(self) -> bytes:
        if (
            self._metadata.description is None
            or self.idempotency_key is None
            or self.etag is None
            or self._metadata.files is None
            or self._metadata.tags is None
        ):
            raise ValueError('Description must be set in metadata to generate manifest.')
        return (
            Manifest(
                name=self._metadata.name or 'Untitled',
                description=self._metadata.description,
                uuid=self.idempotency_key,
                etag=self.etag,
                files=self._metadata.files,
                tags=self._metadata.tags,
            )
            .model_dump_json()
            .encode('utf-8')
        )

    @classmethod
    async def from_file(
        cls,
        path: plb.Path,
        name: str | None = None,
        description: str | None = None,
        user_notes: str | None = None,
    ) -> Self:
        """
        Load a note from a file or directory.
        If path is a directory, looks for NOTE.md, README.md, or index.md.
        """
        target_file = path
        aux_files: dict[str, bytes] = {}

        if path.is_dir():
            # Directory mode: look for main file
            candidates = ['NOTE.md', 'README.md', 'index.md']
            found = False
            for c in candidates:
                if (path / c).exists():
                    target_file = path / c
                    found = True
                    break

            if not found:
                raise FileNotFoundError(
                    f'No note file (NOTE.md, README.md, index.md) found in {path}'
                )

            # Load aux files (simple flat loader for now)
            # TODO: Add recursive asset loading if needed
            for p in path.iterdir():
                if p.is_file() and p.name != target_file.name and not p.name.startswith('.'):
                    aux_files[p.name] = p.read_bytes()

        # Load Template
        template = MemexTemplateFromFile(path=target_file)

        # Permissive Frontmatter Loading
        try:
            fm = await template.frontmatter
        except Exception as e:
            logger.warning(
                'Could not parse frontmatter for %s: %s. Using defaults.', target_file, e
            )
            # Fallback to raw read
            content = target_file.read_bytes()
            return cls(
                name=name or target_file.stem,
                description=description or 'Imported NoteInput',
                content=content,
                files=aux_files,
                user_notes=user_notes,
            )

        files = await template.files
        # Merge dir-scanned files with template-referenced files (template wins)
        if files:
            aux_files.update(files)

        name_ = await template.name
        description_ = await template.description

        # NB: args override template metadata
        final_name = name or name_ or target_file.stem
        final_description = description or description_ or 'Imported NoteInput'

        return cls(
            name=cast(str, final_name),
            description=cast(str, final_description),
            content=fm.content.encode('utf-8'),
            files=aux_files,
            source_uri=str(path.absolute()),
            user_notes=user_notes,
        )


class MemexAPI:
    """
    Main API entrypoint for Memex.
    High-level facade for memory operations, reflection, and retrieval.
    Orchestrates the MetaStore and FileStore using transactions.
    """

    def __init__(
        self,
        embedding_model: FastEmbedder,
        reranking_model: FastReranker,
        ner_model: FastNERModel,
        metastore: AsyncBaseMetaStoreEngine,
        filestore: BaseAsyncFileStore,
        config: MemexConfig,
    ):
        """
        Initialize the Memex API with injected storage engines.

        Args:
            metastore: Initialized (connected) metadata store engine.
            filestore: Initialized (connected) file store engine.
            config: Configuration (Required).
        """
        self.metastore = metastore
        self.filestore = filestore
        self.config = config
        self.embedding_model = embedding_model
        self.reranking_model = reranking_model
        self.ner_model = ner_model

        # Initialize core components
        # 1. LLM
        # NB: We trust the config is valid. If it fails, we let it crash to inform the user.
        if dspy.settings.lm is None:
            model_config = self.config.server.memory.extraction.model
            assert model_config is not None, (
                'extraction.model must be set (via default_model propagation)'
            )
            self.lm = dspy.LM(
                model=model_config.model,
                api_base=str(model_config.base_url).rstrip('/') if model_config.base_url else None,
                api_key=model_config.api_key.get_secret_value() if model_config.api_key else None,
            )
            dspy.settings.configure(lm=self.lm)
        else:
            self.lm = dspy.settings.lm

        # 4. Entity Resolver
        self.entity_resolver = EntityResolver()

        # 5. DSPy Predictor
        self.predictor = dspy.Predict(ExtractSemanticFacts)

        # Initialize Engines
        self._extraction = ExtractionEngine(
            config=self.config.server.memory.extraction,
            lm=self.lm,
            predictor=self.predictor,
            embedding_model=self.embedding_model,
            entity_resolver=self.entity_resolver,
            reflection_config=self.config.server.memory.reflection,
            page_index_lm=self.lm,
        )

        self._retrieval = RetrievalEngine(
            embedder=self.embedding_model,
            reranker=self.reranking_model,
            ner_model=self.ner_model,
            lm=self.lm,
            retrieval_config=self.config.server.memory.retrieval,
            session_factory=self.metastore.session_maker(),
        )

        self._doc_search = NoteSearchEngine(
            embedder=self.embedding_model,
            ner_model=self.ner_model,
            lm=self.lm,
            retrieval_config=self.config.server.memory.retrieval,
        )

        self._contradiction = _build_contradiction_engine(self.config)

        self.memory = MemoryEngine(
            config=self.config,
            extraction_engine=self._extraction,
            retrieval_engine=self._retrieval,
            contradiction_engine=self._contradiction,
            session_factory=self.metastore.session_maker(),
        )

        self.queue_service = ReflectionQueueService(self.config.server.memory.reflection)
        self.batch_manager = JobManager(self)
        self._file_processor = FileContentProcessor()
        # Domain services
        self._vaults = VaultService(
            metastore=self.metastore,
            filestore=self.filestore,
            config=self.config,
        )
        self._stats = StatsService(
            metastore=self.metastore,
            filestore=self.filestore,
            config=self.config,
        )
        self._lineage = LineageService(
            metastore=self.metastore,
            filestore=self.filestore,
            config=self.config,
        )
        self._entities = EntityService(
            metastore=self.metastore,
            filestore=self.filestore,
            config=self.config,
        )
        self._reflection = ReflectionService(
            metastore=self.metastore,
            config=self.config,
            lm=self.lm,
            memory=self.memory,
            extraction=self._extraction,
            queue_service=self.queue_service,
            embedding_model=self.embedding_model,
        )
        self._search = SearchService(
            metastore=self.metastore,
            config=self.config,
            lm=self.lm,
            memory=self.memory,
            doc_search=self._doc_search,
            vaults=self._vaults,
        )
        self._notes = NoteService(
            metastore=self.metastore,
            filestore=self.filestore,
            config=self.config,
            vaults=self._vaults,
        )
        self._kv = KVService(
            metastore=self.metastore,
            filestore=self.filestore,
            config=self.config,
        )
        self._ingestion = IngestionService(
            metastore=self.metastore,
            filestore=self.filestore,
            config=self.config,
            lm=self.lm,
            memory=self.memory,
            file_processor=self._file_processor,
            vaults=self._vaults,
        )

    @property
    def embedder(self) -> FastEmbedder:
        """Alias for embedding_model for backward compatibility."""
        return self.embedding_model

    @embedder.setter
    def embedder(self, value: FastEmbedder):
        self.embedding_model = value

    @property
    def reranker(self) -> FastReranker:
        """Alias for reranking_model for backward compatibility."""
        return self.reranking_model

    @reranker.setter
    def reranker(self, value: FastReranker):
        self.reranking_model = value

    async def initialize(self) -> None:
        """
        Perform async initialization tasks.
        1. Ensure Global Vault exists.
        2. Ensure Active Vault exists.
        """
        from memex_core.memory.sql_models import Vault
        from memex_core.config import GLOBAL_VAULT_NAME

        async with self.metastore.session() as session:
            # 1. Ensure Global Vault
            try:
                vault = await session.get(Vault, GLOBAL_VAULT_ID)
                if not vault:
                    logger.info('Initializing Global Vault...')
                    vault = Vault(
                        id=GLOBAL_VAULT_ID,
                        name=GLOBAL_VAULT_NAME,
                        description='Default global vault for all memories.',
                    )
                    session.add(vault)
                    await session.commit()
                    logger.info('Global Vault created (id: %s).', GLOBAL_VAULT_ID)
            except IntegrityError:
                await session.rollback()
                logger.debug('Global Vault already exists (concurrent creation handled).')

            # 2. Ensure Active Vault (if different from global)
            active_identifier = self.config.server.default_active_vault
            if active_identifier != GLOBAL_VAULT_NAME:
                try:
                    # Check if it exists
                    vault_id = await self.resolve_vault_identifier(active_identifier)
                    logger.info('Active vault: "%s" (id: %s)', active_identifier, vault_id)
                except VaultNotFoundError:
                    logger.info(
                        'Created vault "%s" (auto-initialized from config)', active_identifier
                    )
                    # If it's a UUID string, use it as ID, otherwise use as Name
                    try:
                        v_id = UUID(active_identifier)
                        new_vault = Vault(
                            id=v_id,
                            name=active_identifier,
                            description=f'Auto-initialized vault (ID: {active_identifier})',
                        )
                    except ValueError:
                        new_vault = Vault(
                            name=active_identifier,
                            description=f'Auto-initialized vault: {active_identifier}',
                        )

                    try:
                        session.add(new_vault)
                        await session.commit()
                        logger.info('Vault "%s" created.', active_identifier)
                    except IntegrityError:
                        await session.rollback()
                        logger.debug(
                            f"Vault '{active_identifier}' already exists (concurrent creation handled)."
                        )

        # Clear cache after initialization to ensure resolve_vault_identifier sees new vaults
        _VAULT_RESOLUTION_CACHE.clear()

        # 3. Validate default reader vault (if different from active)
        reader_name = self.config.server.default_reader_vault
        if reader_name != active_identifier:
            try:
                reader_id = await self.resolve_vault_identifier(reader_name)
                logger.info('Default reader vault: "%s" (id: %s)', reader_name, reader_id)
            except VaultNotFoundError:
                logger.warning(
                    'Default reader vault "%s" not found. It will be skipped during retrieval.',
                    reader_name,
                )

        # Reconcile interrupted batch jobs
        try:
            await self.batch_manager.reconcile_interrupted_jobs()
        except Exception as e:
            logger.warning(f'Failed to reconcile batch jobs during initialization: {e}')

    async def validate_vault_exists(self, vault_id: UUID) -> bool:
        """Check if a vault exists. Delegates to VaultService."""
        return await self._vaults.validate_vault_exists(vault_id)

    async def resolve_vault_identifier(self, identifier: UUID | str) -> UUID:
        """Resolves a vault name or UUID string. Delegates to VaultService."""
        return await self._vaults.resolve_vault_identifier(identifier)

    async def ingest_from_url(
        self,
        url: str,
        vault_id: UUID | str | None = None,
        reflect_after: bool = True,
        assets: dict[str, bytes] | None = None,
        user_notes: str | None = None,
    ) -> dict[str, Any]:
        """Ingest from URL. Delegates to IngestionService."""
        return await self._ingestion.ingest_from_url(
            url,
            vault_id=vault_id,
            reflect_after=reflect_after,
            assets=assets,
            user_notes=user_notes,
        )

    async def ingest_from_file(
        self,
        file_path: str | plb.Path,
        vault_id: UUID | str | None = None,
        reflect_after: bool = True,
        note_key: str | None = None,
        user_notes: str | None = None,
    ) -> dict[str, Any]:
        """Ingest from file. Delegates to IngestionService."""
        return await self._ingestion.ingest_from_file(
            file_path,
            vault_id=vault_id,
            reflect_after=reflect_after,
            note_key=note_key,
            user_notes=user_notes,
        )

    async def ingest(
        self,
        note: NoteInput,
        vault_id: UUID | str | None = None,
        event_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Ingest a note. Delegates to IngestionService."""
        return await self._ingestion.ingest(note, vault_id=vault_id, event_date=event_date)

    async def ingest_batch_internal(
        self,
        notes: list[Any],
        vault_id: UUID | str | None = None,
        batch_size: int = 32,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Batch ingestion. Delegates to IngestionService."""
        async for result in self._ingestion.ingest_batch_internal(
            notes, vault_id=vault_id, batch_size=batch_size
        ):
            yield result

    async def get_resource(self, path: str) -> bytes:
        """Direct access to stored assets. Delegates to NoteService."""
        return await self._notes.get_resource(path)

    def get_resource_path(self, path: str) -> str | None:
        """Return absolute filesystem path for a resource, or None for remote stores."""
        return self._notes.get_resource_path(path)

    async def set_note_status(
        self,
        note_id: UUID,
        status: str,
        linked_note_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Set a note's lifecycle status. Delegates to NoteService."""
        return await self._notes.set_note_status(note_id, status, linked_note_id)

    async def update_note_title(self, note_id: UUID, new_title: str) -> dict[str, Any]:
        """Update a note's title. Delegates to NoteService."""
        return await self._notes.update_note_title(note_id, new_title)

    async def update_note_date(self, note_id: UUID, new_date: datetime) -> dict[str, Any]:
        """Update a note's publish_date and cascade to memory units. Delegates to NoteService."""
        return await self._notes.update_note_date(note_id, new_date)

    async def get_note(self, note_id: UUID) -> dict[str, Any]:
        """Retrieve a single document by ID. Delegates to NoteService."""
        return await self._notes.get_note(note_id)

    async def get_note_metadata(self, note_id: UUID) -> dict[str, Any] | None:
        """Retrieve just the metadata from the page index. Delegates to NoteService."""
        return await self._notes.get_note_metadata(note_id)

    async def get_note_page_index(self, note_id: UUID) -> dict[str, Any] | None:
        """Retrieve the page index. Delegates to NoteService."""
        return await self._notes.get_note_page_index(note_id)

    async def get_node(self, node_id: UUID) -> NodeDTO | None:
        """Retrieve a specific document node. Delegates to NoteService."""
        return await self._notes.get_node(node_id)

    async def get_nodes(self, node_ids: list[UUID]) -> list[NodeDTO]:
        """Retrieve multiple document nodes. Delegates to NoteService."""
        return await self._notes.get_nodes(node_ids)

    async def get_notes_metadata(self, note_ids: list[UUID]) -> list[dict[str, Any]]:
        """Retrieve metadata for multiple notes. Delegates to NoteService."""
        return await self._notes.get_notes_metadata(note_ids)

    async def list_notes(
        self,
        limit: int = 100,
        offset: int = 0,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
    ) -> list[Any]:
        """List ingested documents. Delegates to NoteService."""
        return await self._notes.list_notes(
            limit=limit,
            offset=offset,
            vault_id=vault_id,
            vault_ids=vault_ids,
            after=after,
            before=before,
        )

    async def get_stats_counts(
        self,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> dict[str, int]:
        """Get total counts. Delegates to StatsService."""
        return await self._stats.get_stats_counts(vault_id=vault_id, vault_ids=vault_ids)

    async def get_recent_notes(
        self,
        limit: int = 5,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
    ) -> list[Any]:
        """Get the most recent notes. Delegates to NoteService."""
        return await self._notes.get_recent_notes(
            limit=limit,
            vault_id=vault_id,
            vault_ids=vault_ids,
            after=after,
            before=before,
        )

    async def list_entities_ranked(
        self,
        limit: int = 100,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Stream entities ranked by hybrid score. Delegates to EntityService."""
        async for entity in self._entities.list_entities_ranked(
            limit=limit, vault_ids=vault_ids, entity_type=entity_type
        ):
            yield entity

    async def get_entity_cooccurrences(
        self,
        entity_id: UUID | str,
        vault_ids: list[UUID] | None = None,
        limit: int = 50,
    ) -> list[Any]:
        """Get co-occurrence edges for an entity. Delegates to EntityService."""
        return await self._entities.get_entity_cooccurrences(
            entity_id, vault_ids=vault_ids, limit=limit
        )

    async def get_bulk_cooccurrences(
        self, entity_ids: list[UUID], vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """Get co-occurrences between a set of entities. Delegates to EntityService."""
        return await self._entities.get_bulk_cooccurrences(entity_ids, vault_ids=vault_ids)

    async def get_entity_mentions(
        self, entity_id: UUID | str, limit: int = 20, vault_ids: list[UUID] | None = None
    ) -> list[dict[str, Any]]:
        """Get entity mentions. Delegates to EntityService."""
        return await self._entities.get_entity_mentions(entity_id, limit=limit, vault_ids=vault_ids)

    async def get_entity(self, entity_id: UUID | str, vault_id: UUID | None = None) -> Any | None:
        """Get an entity by ID. Delegates to EntityService."""
        return await self._entities.get_entity(entity_id, vault_id=vault_id)

    async def get_entities(self, entity_ids: list[UUID], vault_id: UUID | None = None) -> list[Any]:
        """Get multiple entities by ID. Delegates to EntityService."""
        return await self._entities.get_entities(entity_ids, vault_id=vault_id)

    async def get_memory_unit(self, unit_id: UUID | str) -> Any | None:
        """Get a memory unit by ID. Delegates to StatsService."""
        return await self._stats.get_memory_unit(unit_id)

    async def delete_memory_unit(self, unit_id: UUID) -> bool:
        """Delete a memory unit. Delegates to StatsService."""
        return await self._stats.delete_memory_unit(unit_id)

    async def retrieve(self, request: RetrievalRequest) -> tuple[list[MemoryUnit], Any]:
        """Retrieve memories using TEMPR Recall. Delegates to SearchService."""
        return await self._search.retrieve(request)

    async def search(
        self,
        query: str,
        limit: int = 10,
        vault_ids: list[UUID | str] | None = None,
        token_budget: int | None = None,
        strategies: list[str] | None = None,
        include_stale: bool = False,
        include_superseded: bool = False,
        debug: bool = False,
        after: datetime | None = None,
        before: datetime | None = None,
        tags: list[str] | None = None,
    ) -> tuple[list[MemoryUnit], Any]:
        """Search with reranking. Delegates to SearchService."""
        return await self._search.search(
            query=query,
            limit=limit,
            vault_ids=vault_ids,
            token_budget=token_budget,
            strategies=strategies,
            include_stale=include_stale,
            include_superseded=include_superseded,
            debug=debug,
            after=after,
            before=before,
            tags=tags,
        )

    async def summarize_search_results(self, query: str, texts: list[str]) -> str:
        """Summarize search results. Delegates to SearchService."""
        return await self._search.summarize_search_results(query, texts)

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
        after: datetime | None = None,
        before: datetime | None = None,
        tags: list[str] | None = None,
    ) -> list[NoteSearchResult]:
        """Search notes. Delegates to SearchService."""
        return await self._search.search_notes(
            query=query,
            limit=limit,
            vault_ids=vault_ids,
            expand_query=expand_query,
            fusion_strategy=fusion_strategy,
            strategies=strategies,
            strategy_weights=strategy_weights,
            reason=reason,
            summarize=summarize,
            mmr_lambda=mmr_lambda,
            after=after,
            before=before,
            tags=tags,
        )

    async def resolve_source_notes(self, unit_ids: list[UUID]) -> dict[UUID, UUID]:
        """Resolve source note IDs. Delegates to SearchService."""
        return await self._search.resolve_source_notes(unit_ids)

    async def background_reflect(self, request: ReflectionRequest) -> None:
        """Run background reflection. Delegates to ReflectionService."""
        await self._reflection.background_reflect(request)

    async def background_reflect_batch(self, requests: list[ReflectionRequest]) -> None:
        """Run background batch reflection. Delegates to ReflectionService."""
        await self._reflection.background_reflect_batch(requests)

    async def reflect(self, request: ReflectionRequest) -> ReflectionResult:
        """Reflect on a single entity. Delegates to ReflectionService."""
        return await self._reflection.reflect(request)

    async def reflect_batch(self, requests: list[ReflectionRequest]) -> list[ReflectionResult]:
        """Reflect on multiple entities. Delegates to ReflectionService."""
        return await self._reflection.reflect_batch(requests)

    async def create_vault(self, name: str, description: str | None = None) -> Any:
        """Create a new vault. Delegates to VaultService."""
        return await self._vaults.create_vault(name, description)

    async def delete_vault(self, vault_id: UUID) -> bool:
        """Delete a vault. Delegates to VaultService."""
        return await self._vaults.delete_vault(vault_id)

    async def delete_note(self, note_id: UUID) -> bool:
        """Delete a document and all associated data. Delegates to NoteService."""
        return await self._notes.delete_note(note_id)

    async def migrate_note(self, note_id: UUID, target_vault_id: UUID | str) -> dict[str, Any]:
        """Move a note to a different vault. Delegates to NoteService."""
        resolved_id = await self._vaults.resolve_vault_identifier(target_vault_id)
        return await self._notes.migrate_note(note_id, resolved_id)

    async def delete_entity(self, entity_id: UUID) -> bool:
        """Delete an entity. Delegates to EntityService."""
        return await self._entities.delete_entity(entity_id)

    async def delete_mental_model(self, entity_id: UUID, vault_id: UUID) -> bool:
        """Delete a mental model. Delegates to EntityService."""
        return await self._entities.delete_mental_model(entity_id, vault_id)

    async def list_vaults(self) -> list[Any]:
        """List all vaults. Delegates to VaultService."""
        return await self._vaults.list_vaults()

    async def list_vaults_with_counts(self) -> list[dict[str, Any]]:
        """List all vaults with note counts. Delegates to VaultService."""
        return await self._vaults.list_vaults_with_counts()

    async def get_vault_by_name(self, name: str) -> Any | None:
        """Get a vault by name. Delegates to VaultService."""
        return await self._vaults.get_vault_by_name(name)

    async def get_reflection_queue_batch(
        self,
        limit: int = 10,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> list[Any]:
        """Get reflection queue batch. Delegates to ReflectionService."""
        return await self._reflection.get_reflection_queue_batch(
            limit=limit, vault_id=vault_id, vault_ids=vault_ids
        )

    async def claim_reflection_queue_batch(
        self, limit: int = 10, vault_id: UUID | None = None
    ) -> list[Any]:
        """Claim reflection queue batch. Delegates to ReflectionService."""
        return await self._reflection.claim_reflection_queue_batch(limit=limit, vault_id=vault_id)

    async def get_dead_letter_items(
        self,
        limit: int = 50,
        offset: int = 0,
        vault_id: UUID | None = None,
    ) -> list[Any]:
        """List dead-lettered reflection tasks. Delegates to ReflectionService."""
        return await self._reflection.get_dead_letter_items(
            limit=limit, offset=offset, vault_id=vault_id
        )

    async def retry_dead_letter_item(self, item_id: UUID) -> Any:
        """Retry a dead-lettered reflection task. Delegates to ReflectionService."""
        return await self._reflection.retry_dead_letter_item(item_id)

    async def get_top_entities(
        self,
        limit: int = 5,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
    ) -> list[Any]:
        """Get top entities by mention count. Delegates to EntityService."""
        return await self._entities.get_top_entities(
            limit=limit, vault_ids=vault_ids, entity_type=entity_type
        )

    async def search_entities(
        self,
        query: str,
        limit: int = 10,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
    ) -> list[Any]:
        """Search entities by name. Delegates to EntityService."""
        return await self._entities.search_entities(
            query, limit=limit, vault_ids=vault_ids, entity_type=entity_type
        )

    async def get_lineage(
        self,
        entity_type: str,
        entity_id: UUID | str,
        direction: LineageDirection = LineageDirection.UPSTREAM,
        depth: int = 3,
        limit: int = 10,
    ) -> LineageResponse:
        """Retrieve the full lineage (dependency chain) of a specific entity.

        Delegates to LineageService.
        """
        return await self._lineage.get_lineage(
            entity_type=entity_type,
            entity_id=entity_id,
            direction=direction,
            depth=depth,
            limit=limit,
        )

    # --- Note title search ---

    async def find_notes_by_title(
        self,
        query: str,
        vault_ids: list[UUID] | None = None,
        limit: int = 5,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Fuzzy-search notes by title. Delegates to NoteService."""
        return await self._notes.find_notes_by_title(
            query=query, vault_ids=vault_ids, limit=limit, threshold=threshold
        )

    # --- Embeddings ---

    async def embed_text(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text.

        Exposes the embedding model through the public API so that callers
        (MCP, CLI) do not need to import core internals.
        """
        result = await asyncio.to_thread(self.embedding_model.encode, [text])
        return result[0].tolist()

    # --- KV store ---

    async def kv_put(
        self,
        key: str,
        value: str,
        embedding: list[float] | None = None,
    ) -> Any:
        """Upsert a KV entry. Delegates to KVService."""
        return await self._kv.put(key=key, value=value, embedding=embedding)

    async def kv_get(self, key: str) -> Any | None:
        """Get a KV entry by key. Delegates to KVService."""
        return await self._kv.get(key=key)

    async def kv_search(
        self,
        query_embedding: list[float],
        namespaces: list[str] | None = None,
        limit: int = 5,
    ) -> list[Any]:
        """Semantic search over KV entries. Delegates to KVService."""
        return await self._kv.search(
            query_embedding=query_embedding, namespaces=namespaces, limit=limit
        )

    async def kv_delete(self, key: str) -> bool:
        """Delete a KV entry. Delegates to KVService."""
        return await self._kv.delete(key=key)

    async def kv_list(
        self,
        namespaces: list[str] | None = None,
        limit: int = 100,
        exclude_prefix: str | None = None,
        key_prefix: str | None = None,
        pattern: str | None = None,
    ) -> list[Any]:
        """List KV entries. Delegates to KVService."""
        return await self._kv.list_entries(
            namespaces=namespaces,
            limit=limit,
            exclude_prefix=exclude_prefix,
            key_prefix=key_prefix,
            pattern=pattern,
        )
