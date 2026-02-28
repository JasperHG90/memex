from typing import TypeVar, cast, Self, Any, AsyncGenerator
import hashlib
import pathlib as plb
import logging
import asyncio
import base64
from uuid import UUID
from functools import cached_property
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

import dspy
from sqlmodel import col

from memex_common.exceptions import (
    VaultNotFoundError,
    ResourceNotFoundError,
    NoteNotFoundError,
)
from memex_common.schemas import (
    LineageResponse,
    LineageDirection,
    NoteSearchRequest,
    NoteSearchResult,
    NodeDTO,
)
from memex_core.config import MemexConfig, GLOBAL_VAULT_ID
from memex_core.models import NoteMetadata
from memex_core.storage import (
    calculate_deep_hash,
    Manifest,
)
from memex_core.storage.transaction import AsyncTransaction
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.templates import MemexTemplateFromFile

# Engines and Models
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.models import RetainContent
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.retrieval.document_search import NoteSearchEngine
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.reflect.models import (
    ReflectionRequest,
    ReflectionResult,
    OpinionFormationRequest,
)
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.memory.sql_models import MemoryUnit, Observation
from memex_core.memory.models.embedding import FastEmbedder
from memex_core.memory.models.reranking import FastReranker
from memex_core.memory.models.ner import FastNERModel
from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.extraction.core import ExtractSemanticFacts
from memex_core.processing.web import WebContentProcessor
from memex_core.processing.files import FileContentProcessor
from memex_core.processing.batch import JobManager
from memex_core.processing.dates import extract_document_date
from memex_core.processing.titles import resolve_document_title
from memex_core.llm import run_dspy_operation
from memex_core.services.entities import EntityService
from memex_core.services.lineage import LineageService
from memex_core.services.stats import StatsService
from memex_core.services.vaults import VaultService, _VAULT_RESOLUTION_CACHE

logger = logging.getLogger('memex.core.api')

T = TypeVar('T')


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
    ):
        self._metadata = NoteMetadata(name=name, description=description)
        self._content = content
        self._files = files or {}
        self.source_uri = source_uri
        self.original_content_hash = original_content_hash
        self._explicit_key = note_key
        # Update metadata fields
        self._metadata.update('files', list(self._files.keys()))
        self._metadata.update('tags', tags or [])
        self._metadata.update('etag', self.etag)
        self._metadata.update('uuid', self.uuid)

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
    def uuid(self) -> str:
        """Backward-compatible alias for note_key."""
        return self.note_key

    @classmethod
    def calculate_uuid_from_dto(cls, dto: Any) -> str:
        """Calculate the UUID (note_key) for a NoteCreateDTO without full instantiation."""
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
        )
        return temp_note.uuid

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
        )
        return temp_note.content_fingerprint

    @cached_property
    def manifest(self) -> bytes:
        if (
            self._metadata.description is None
            or self.uuid is None
            or self.etag is None
            or self._metadata.files is None
            or self._metadata.tags is None
        ):
            raise ValueError('Description must be set in metadata to generate manifest.')
        return (
            Manifest(
                name=self._metadata.name or 'Untitled',
                description=self._metadata.description,
                uuid=self.uuid,
                etag=self.etag,
                files=self._metadata.files,
                tags=self._metadata.tags,
            )
            .model_dump_json()
            .encode('utf-8')
        )

    @classmethod
    async def from_file(
        cls, path: plb.Path, name: str | None = None, description: str | None = None
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
                api_base=str(model_config.base_url) if model_config.base_url else None,
                api_key=model_config.api_key.get_secret_value() if model_config.api_key else None,
            )
            dspy.settings.configure(lm=self.lm)
        else:
            self.lm = dspy.settings.lm

        # 4. Entity Resolver
        self.entity_resolver = EntityResolver(
            resolution_threshold=self.config.server.memory.opinion_formation.confidence.similarity_threshold
        )

        # 5. DSPy Predictor
        self.predictor = dspy.Predict(ExtractSemanticFacts)

        # Initialize Engines
        self._extraction = ExtractionEngine(
            config=self.config.server.memory.extraction,
            confidence_config=self.config.server.memory.opinion_formation.confidence,
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
        )

        self._doc_search = NoteSearchEngine(
            embedder=self.embedding_model,
            ner_model=self.ner_model,
            lm=self.lm,
        )

        self.memory = MemoryEngine(
            config=self.config,
            extraction_engine=self._extraction,
            retrieval_engine=self._retrieval,
        )

        self.queue_service = ReflectionQueueService(self.config.server.memory.reflection)
        self.batch_manager = JobManager(self)
        self._file_processor = FileContentProcessor()
        self._reflection_lock = asyncio.Lock()

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
            active_identifier = self.config.server.active_vault
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

        # 3. Validate attached vaults
        for av_name in self.config.server.attached_vaults:
            try:
                av_id = await self.resolve_vault_identifier(av_name)
                logger.info('Attached vault: "%s" (id: %s)', av_name, av_id)
            except VaultNotFoundError:
                logger.warning(
                    'Attached vault "%s" not found. It will be skipped during retrieval.',
                    av_name,
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
    ) -> dict[str, Any]:
        """Ingest content from a URL and store it as a NoteInput."""

        try:
            extracted = await WebContentProcessor.fetch_and_extract(url)
        except ValueError as e:
            logger.error(f'Failed to fetch {url}: {e}')
            raise

        target_vault_id = await self.resolve_vault_identifier(
            vault_id or self.config.server.active_vault
        )

        title = extracted.metadata.get('title') or None  # None triggers title resolution
        now = datetime.now().isoformat()

        note_content = f"""---
source_url: {extracted.source}
hostname: {extracted.metadata.get('hostname')}
ingested_at: {now}
publish_date: {extracted.metadata.get('date')}
---
{extracted.content}
"""

        original_hash = hashlib.md5(extracted.content.encode('utf-8')).hexdigest()

        decoded_assets = {}
        if assets:
            for k, v in assets.items():
                try:
                    decoded_assets[k] = base64.b64decode(v)
                except Exception as e:
                    logger.debug('Base64 decode failed for asset %r, using raw value: %s', k, e)
                    decoded_assets[k] = v

        note = NoteInput(
            name=title,
            description=f'Content from {extracted.metadata.get("hostname", url)}',
            content=note_content.encode('utf-8'),
            source_uri=url,
            original_content_hash=original_hash,
            files=decoded_assets,
        )

        # Resolve document date: web metadata -> LLM fallback -> now()
        event_date = extracted.document_date
        if event_date is None:
            async with self.metastore.session() as date_session:
                event_date = await extract_document_date(
                    extracted.content, self.lm, date_session, target_vault_id
                )
                await date_session.commit()

        return await self.ingest(note, vault_id=target_vault_id, event_date=event_date)

    async def ingest_from_file(
        self,
        file_path: str | plb.Path,
        vault_id: UUID | str | None = None,
        reflect_after: bool = True,
    ) -> dict[str, Any]:
        """
        Ingest content from a path.
        - If it's a directory or a .md file, it's treated as a native NoteInput (preserving structure/assets).
        - Otherwise, it uses MarkItDown for extraction and summarizes it into a new NoteInput.
        """
        path = plb.Path(file_path)

        if path.is_dir() or path.suffix.lower() == '.md':
            logger.info(f'Ingesting {path} as a native NoteInput.')
            note = await NoteInput.from_file(path)
            return await self.ingest(note)

        try:
            extracted = await self._file_processor.extract(path)
        except Exception as e:
            logger.error(f'Failed to extract {path}: {e}')
            raise

        target_vault_id = await self.resolve_vault_identifier(
            vault_id or self.config.server.active_vault
        )

        now = datetime.now().isoformat()

        note_content = f"""---
source_file: {path.name}
type: {extracted.content_type}
ingested_at: {now}
---
{extracted.content}
"""

        original_hash = hashlib.md5(extracted.content.encode('utf-8')).hexdigest()

        note = NoteInput(
            name=path.stem,
            description=f'Content from {path.name}',
            content=note_content.encode('utf-8'),
            tags=['file-import'],
            source_uri=str(path.absolute()),
            original_content_hash=original_hash,
            files=extracted.images,
        )

        # Resolve document date: file mtime -> LLM fallback -> now()
        event_date = extracted.document_date
        if event_date is None:
            async with self.metastore.session() as date_session:
                event_date = await extract_document_date(
                    extracted.content, self.lm, date_session, target_vault_id
                )
                await date_session.commit()

        return await self.ingest(note, vault_id=target_vault_id, event_date=event_date)

    async def ingest(
        self,
        note: NoteInput,
        vault_id: UUID | str | None = None,
        event_date: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Transactional ingestion of a note into Memex.

        Workflow:
        1. Calculate ID (NoteInput.uuid).
        2. Idempotency Check: Skip if exists in MetaStore.
        3. Transaction: Open AsyncTransaction.
        4. Stage Files: Save to FileStore.
        5. Extract Facts: Run MemoryEngine.retain in DB session.
        6. Commit: 2PC via AsyncTransaction.

        Args:
            note: The NoteInput object to ingest.
            vault_id: Optional target vault identifier. If None, uses active_vault.
            event_date: Optional document date for temporal anchoring. Falls back to now().

        Returns:
            Dict containing extraction results (unit_ids, etc.) or status.
        """
        note_uuid = note.uuid
        logger.info(f'Ingesting note: {note._metadata.name} (UUID: {note_uuid})')

        # Determine Target Vault
        target_vault_id = await self.resolve_vault_identifier(
            vault_id or self.config.server.active_vault
        )

        # 2. Two-Gate Idempotency Check
        async with self.metastore.session() as session:
            # Fetch vault name for path organization
            from memex_core.memory.sql_models import Vault, Note

            vault = await session.get(Vault, target_vault_id)
            vault_name = vault.name if vault else str(target_vault_id)

            # Gate 1: Does note_key exist?
            from sqlmodel import select

            stmt = select(Note.content_hash).where(col(Note.id) == note_uuid)
            stored_hash = (await session.exec(stmt)).first()
            if stored_hash is not None:
                # Gate 2: Has content_fingerprint changed?
                if stored_hash == note.content_fingerprint:
                    logger.info(f'Document {note_uuid} unchanged. Skipping ingestion.')
                    return {'status': 'skipped', 'reason': 'idempotency_check'}
                logger.info(f'Document {note_uuid} exists but content changed. Incremental update.')

        # 3. Open Transaction
        async with AsyncTransaction(self.metastore, self.filestore, note_uuid) as txn:
            # 4. Stage Files (FS)
            # We save ONLY auxiliary files (assets)
            # Path structure: assets/{vault_name}/{uuid}/filename
            asset_path = f'assets/{vault_name}/{note_uuid}'
            asset_files_list = []

            # Save auxiliary files
            for filename, content in note._files.items():
                full_asset_key = f'{asset_path}/{filename}'
                await self.filestore.save(full_asset_key, content)
                asset_files_list.append(full_asset_key)

            # 5. Extract Facts (MS)
            content_text = note._content.decode('utf-8')

            # Resolve the best available title before building the payload.
            resolved_title = await resolve_document_title(
                content_text,
                note._metadata.name,
                self.lm,
                session=txn.db_session,
                vault_id=target_vault_id,
            )

            retain_content = RetainContent(
                content=content_text,
                event_date=event_date or datetime.now(timezone.utc),
                payload={
                    'source': 'note',
                    'note_name': resolved_title,
                    'note_description': note._metadata.description,
                    'uuid': note_uuid,
                    # We pass the asset path if assets exist
                    'filestore_path': asset_path if asset_files_list else None,
                    'assets': asset_files_list,
                    'source_uri': note.source_uri,
                    'content_fingerprint': note.content_fingerprint,
                },
                vault_id=target_vault_id,
            )

            # Use the transaction's DB session
            result = await self.memory.retain(
                session=txn.db_session,
                contents=[retain_content],
                note_id=note_uuid,
                reflect_after=False,
                agent_name='user',
            )

            # Inject note_id into result for clients
            result['note_id'] = note_uuid
            result['status'] = 'success'

            # Transaction commit happens on exit
            return result

    async def ingest_batch_internal(
        self,
        notes: list[Any],
        vault_id: UUID | str | None = None,
        batch_size: int = 32,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        Internal high-performance batch ingestion logic.
        Orchestrates idempotency checks, asset staging, and batched memory retention.
        Yields progress updates.

        Args:
            notes: List of NoteDTO objects.
            vault_id: Optional target vault identifier.
            batch_size: Processing chunk size.

        Yields:
            Aggregated results (processed, skipped, failed counts and document IDs).
        """
        from memex_core.memory.sql_models import Vault, Note
        from sqlmodel import select

        target_vault_id = await self.resolve_vault_identifier(
            vault_id or self.config.server.active_vault
        )

        # 1. Resolve Vault Name for path organization
        async with self.metastore.session() as session:
            vault = await session.get(Vault, target_vault_id)
            vault_name = vault.name if vault else str(target_vault_id)

        # 2. Two-Gate Idempotency Check
        # Gate 1: Does note_key exist? Gate 2: Has content_fingerprint changed?
        note_uuids = [UUID(NoteInput.calculate_uuid_from_dto(n)) for n in notes]
        note_fingerprints = [NoteInput.calculate_fingerprint_from_dto(n) for n in notes]
        async with self.metastore.session() as session:
            stmt = select(Note.id, Note.content_hash).where(col(Note.id).in_(note_uuids))
            db_result = await session.exec(stmt)
            # Map note_key -> stored content_hash
            existing_docs: dict[UUID, str | None] = {row[0]: row[1] for row in db_result.all()}

        results: dict[str, Any] = {
            'processed_count': 0,
            'skipped_count': 0,
            'failed_count': 0,
            'note_ids': [],
            'errors': [],
        }

        # Filter: skip only if note_key exists AND content_fingerprint matches
        to_process = []
        for i, (note_dto, note_uuid, fingerprint) in enumerate(
            zip(notes, note_uuids, note_fingerprints)
        ):
            if note_uuid in existing_docs:
                stored_hash = existing_docs[note_uuid]
                if stored_hash == fingerprint:
                    # Gate 2 match: exact same content, skip
                    results['skipped_count'] += 1
                    continue
                # Gate 2 mismatch: content changed, needs incremental update
            to_process.append((i, note_dto, note_uuid))

        # Initial yield for skipped items
        yield results

        # 3. Batch Processing Loop
        for i in range(0, len(to_process), batch_size):
            chunk = to_process[i : i + batch_size]

            # Chunk Atomicity via AsyncTransaction
            # We process one transaction per chunk
            try:
                # We use the UUID of the first note in the chunk as a placeholder for the txn ID
                # since AsyncTransaction expects a single UUID for its lineage.
                # TODO: Revisit if AsyncTransaction needs a multi-UUID mode.
                chunk_txn_id = chunk[0][2]

                async with AsyncTransaction(self.metastore, self.filestore, chunk_txn_id) as txn:
                    chunk_doc_ids = []

                    for original_idx, note_dto, note_uuid in chunk:
                        # Decode and Stage Assets
                        asset_path = f'assets/{vault_name}/{note_uuid}'
                        asset_files_list = []

                        # Content is already bytes
                        decoded_content = note_dto.content_decoded

                        for filename, content in note_dto.files.items():
                            # content is Base64 encoded bytes in NoteDTO.
                            # We must decode it to store raw files (e.g. images)
                            try:
                                raw_content = base64.b64decode(content)
                            except Exception as e:
                                logger.debug(
                                    'Base64 decode failed for file %r, using raw: %s', filename, e
                                )
                                raw_content = content

                            full_asset_key = f'{asset_path}/{filename}'
                            await self.filestore.save(full_asset_key, raw_content)
                            asset_files_list.append(full_asset_key)

                        # Prepare RetainContent
                        retain_content = RetainContent(
                            content=decoded_content,
                            event_date=datetime.now(timezone.utc),
                            payload={
                                'source': 'batch_note',
                                'note_name': note_dto.name,
                                'note_description': note_dto.description,
                                'uuid': str(note_uuid),
                                'filestore_path': asset_path if asset_files_list else None,
                                'assets': asset_files_list,
                                'content_fingerprint': note_fingerprints[original_idx],
                            },
                            vault_id=target_vault_id,
                        )

                        # Retain in MetaStore per note to ensure document tracking
                        await self.memory.retain(
                            session=txn.db_session,
                            contents=[retain_content],
                            note_id=str(note_uuid),
                            reflect_after=False,
                            agent_name='user',
                        )
                        chunk_doc_ids.append(note_uuid)

                    # Update aggregate results
                    results['processed_count'] += len(chunk)
                    results['note_ids'].extend([str(uid) for uid in chunk_doc_ids])

            except Exception as e:
                logger.error(f'Failed to process ingestion chunk: {e}', exc_info=True)
                results['failed_count'] += len(chunk)
                results['errors'].append({'chunk_start': i, 'error': str(e)})
                # Continue with next chunk

            # Yield progress after each chunk
            yield results

    async def get_resource(self, path: str) -> bytes:
        """
        Direct access to stored assets in the file store.
        """
        return await self.filestore.load(path)

    async def get_note(self, note_id: UUID) -> dict[str, Any]:
        """
        Retrieve a single document by ID.
        """
        from memex_core.memory.sql_models import Note

        async with self.metastore.session() as session:
            doc = await session.get(Note, note_id)
            if not doc:
                raise ResourceNotFoundError(f'Document {note_id} not found.')

            return doc.model_dump()

    async def get_note_page_index(self, note_id: UUID) -> dict[str, Any] | None:
        """Retrieve the page index (slim tree) for a document, or None if not indexed."""
        from memex_core.memory.sql_models import Note

        async with self.metastore.session() as session:
            doc = await session.get(Note, note_id)
            if not doc:
                raise ResourceNotFoundError(f'Document {note_id} not found.')
            return doc.page_index

    async def get_node(self, node_id: UUID) -> NodeDTO | None:
        """Retrieve a specific document node by its ID."""
        from memex_core.memory.sql_models import Node

        async with self.metastore.session() as session:
            node = await session.get(Node, node_id)
            if node is None:
                return None
            return NodeDTO.model_validate(node)

    async def list_notes(
        self,
        limit: int = 100,
        offset: int = 0,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> list[Any]:
        """
        List ingested documents.
        Filters by the given vault_id(s), or the active vault (write target) if not provided.
        """
        from memex_core.memory.sql_models import Note
        from sqlmodel import select

        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)

        async with self.metastore.session() as session:
            stmt = select(Note)
            if ids:
                stmt = stmt.where(col(Note.vault_id).in_(ids))
            else:
                target_vault_id = await self.resolve_vault_identifier(
                    self.config.server.active_vault
                )
                stmt = stmt.where(col(Note.vault_id) == target_vault_id)

            stmt = stmt.offset(offset).limit(limit)
            return list((await session.exec(stmt)).all())

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
    ) -> list[Any]:
        """
        Get the most recent notes.
        """
        from memex_core.memory.sql_models import Note
        from sqlmodel import desc, select

        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)

        async with self.metastore.session() as session:
            stmt = select(Note).order_by(desc(Note.created_at))
            if ids:
                stmt = stmt.where(col(Note.vault_id).in_(ids))
            stmt = stmt.limit(limit)
            return list((await session.exec(stmt)).all())

    async def list_entities_ranked(
        self, limit: int = 100, vault_ids: list[UUID] | None = None
    ) -> AsyncGenerator[Any, None]:
        """Stream entities ranked by hybrid score. Delegates to EntityService."""
        async for entity in self._entities.list_entities_ranked(limit=limit, vault_ids=vault_ids):
            yield entity

    async def get_entity_cooccurrences(
        self, entity_id: UUID | str, vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """Get co-occurrence edges for an entity. Delegates to EntityService."""
        return await self._entities.get_entity_cooccurrences(entity_id, vault_ids=vault_ids)

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

    async def get_entity(self, entity_id: UUID | str) -> Any | None:
        """Get an entity by ID. Delegates to EntityService."""
        return await self._entities.get_entity(entity_id)

    async def get_memory_unit(self, unit_id: UUID | str) -> Any | None:
        """Get a memory unit by ID. Delegates to StatsService."""
        return await self._stats.get_memory_unit(unit_id)

    async def delete_memory_unit(self, unit_id: UUID) -> bool:
        """Delete a memory unit. Delegates to StatsService."""
        return await self._stats.delete_memory_unit(unit_id)

    async def get_daily_token_usage(self) -> list[dict[str, Any]]:
        """Get daily aggregated token usage. Delegates to StatsService."""
        return await self._stats.get_daily_token_usage()

    async def retrieve(self, request: RetrievalRequest) -> list[MemoryUnit]:
        """
        Retrieve memories and synthesized observations using TEMPR Recall.
        """
        async with self.metastore.session() as session:
            return await self.memory.recall(session, request)

    async def search(
        self,
        query: str,
        limit: int = 10,
        skip_opinion_formation: bool = False,
        vault_ids: list[UUID | str] | None = None,
        token_budget: int | None = None,
        strategies: list[str] | None = None,
        include_stale: bool = False,
        debug: bool = False,
    ) -> list[MemoryUnit]:
        """
        Convenience method for search with reranking.
        Scopes to active vault + attached vaults if vault_ids is not provided.

        Args:
            query: User search query.
            limit: Number of results.
            skip_opinion_formation: If True, skips the automated opinion formation loop.
            vault_ids: Optional list of vault IDs to search in.
            token_budget: Optional token budget override.
            strategies: Optional inclusion list of strategies to run.
            include_stale: Whether to include stale memory units.
            debug: When True, attach per-strategy attribution to results.
        """
        vaults = []

        if vault_ids:
            for v in vault_ids:
                vaults.append(await self.resolve_vault_identifier(str(v)))
        else:
            # 1. Active Vault (Write Target) - always included
            vaults.append(await self.resolve_vault_identifier(self.config.server.active_vault))

        request = RetrievalRequest(
            query=query,
            limit=limit,
            vault_ids=vaults,
            token_budget=token_budget,
            strategies=strategies,
            include_stale=include_stale,
            debug=debug,
        )

        async with self.metastore.session() as session:
            return await self.memory.recall(session, request)

    async def summarize_search_results(self, query: str, texts: list[str]) -> str:
        """Synthesize search results into a concise answer with citations.

        Args:
            query: The original search query.
            texts: List of search result texts to summarize.

        Returns:
            AI-generated summary string with bracket citations.
        """
        from memex_core.memory.retrieval.prompts import SearchSummarySignature

        predictor = dspy.Predict(SearchSummarySignature)

        async with self.metastore.session() as session:
            prediction, _ = await run_dspy_operation(
                lm=self.lm,
                predictor=predictor,
                input_kwargs={'query': query, 'search_results': texts},
                session=session,
                context_metadata={'operation': 'search_summary'},
            )
            await session.commit()

        return prediction.summary

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
    ) -> list[NoteSearchResult]:
        """
        Search for documents containing relevant information using raw chunks.
        """
        vaults = []
        if vault_ids:
            for v in vault_ids:
                vaults.append(await self.resolve_vault_identifier(str(v)))
        else:
            vaults.append(await self.resolve_vault_identifier(self.config.server.active_vault))

        kwargs: dict[str, Any] = {}
        if strategies is not None:
            kwargs['strategies'] = strategies
        if strategy_weights is not None:
            kwargs['strategy_weights'] = strategy_weights

        # Resolve effective mmr_lambda: per-request override, else config default
        effective_mmr_lambda = mmr_lambda
        if effective_mmr_lambda is None:
            effective_mmr_lambda = self.config.server.document.mmr_lambda
        if effective_mmr_lambda is not None:
            kwargs['mmr_lambda'] = effective_mmr_lambda

        request = NoteSearchRequest(
            query=query,
            limit=limit,
            vault_ids=vaults,
            expand_query=expand_query,
            fusion_strategy=fusion_strategy,
            reason=reason,
            summarize=summarize,
            **kwargs,
        )

        async with self.metastore.session() as session:
            return await self._doc_search.search(session, request)

    async def resolve_source_notes(self, unit_ids: list[UUID]) -> dict[UUID, UUID]:
        """
        Resolve the source note ID for a list of Memory Unit IDs.
        Returns a map of {unit_id: note_id}.
        """
        from memex_core.memory.sql_models import MemoryUnit
        from sqlmodel import select

        if not unit_ids:
            return {}

        async with self.metastore.session() as session:
            stmt = select(MemoryUnit.id, MemoryUnit.note_id).where(col(MemoryUnit.id).in_(unit_ids))
            results = (await session.exec(stmt)).all()

            return {row[0]: row[1] for row in results if row[1] is not None}

    async def process_opinion_formation(
        self, query: str, context: list[MemoryUnit], vault_id: UUID
    ) -> None:
        """
        Process the opinion formation loop.
        Synthesizes an answer and forms opinions.
        Intended to be capable of running as a background task.
        """
        # We need an answer to form an opinion (CARA loop).
        # Synthesize a quick answer from context.
        answer = await self._synthesize_answer(query, context)

        op_request = OpinionFormationRequest(
            query=query, context=context, answer=answer, vault_id=vault_id
        )
        await self.form_opinions(op_request)

    async def process_opinion_formation_minimal(
        self, query: str, context: list[dict], vault_id: UUID
    ) -> None:
        """
        Process opinion formation with minimal context to prevent memory leaks.
        Receives only lightweight dicts and fetches units by ID in a fresh session,
        instead of holding full MemoryUnit objects in the background task.
        """
        from memex_core.memory.reflect.models import OpinionFormationRequest

        unit_ids = [UUID(c['id']) for c in context if 'id' in c]

        async with self.metastore.session() as session:
            from sqlmodel import select, col
            from memex_core.memory.sql_models import MemoryUnit

            stmt = select(MemoryUnit).where(col(MemoryUnit.id).in_(unit_ids))
            result = await session.exec(stmt)
            fresh_units = list(result.all())

            if not fresh_units:
                return

            answer = await self._synthesize_answer(query, fresh_units)

            op_request = OpinionFormationRequest(
                query=query, context=fresh_units, answer=answer, vault_id=vault_id
            )

            await self.memory.form_opinions(session, op_request)
            await session.commit()

    async def background_reflect(self, request: ReflectionRequest) -> None:
        """
        Run reflection in the background, ensuring serialization via lock.
        """
        async with self._reflection_lock:
            try:
                logger.info(f'Starting background reflection for entity {request.entity_id}')
                await self.reflect(request)
                logger.info(f'Completed background reflection for entity {request.entity_id}')
            except Exception as e:
                logger.error(
                    f'Error during background reflection for entity {request.entity_id}: {e}',
                    exc_info=True,
                )

    async def background_reflect_batch(self, requests: list[ReflectionRequest]) -> None:
        """
        Run batch reflection in the background, ensuring serialization via lock.
        """
        if not requests:
            return

        async with self._reflection_lock:
            try:
                entity_ids = [str(r.entity_id) for r in requests]
                logger.info(f'Starting background batch reflection for entities: {entity_ids}')
                await self.reflect_batch(requests)
                logger.info(f'Completed background batch reflection for {len(requests)} entities')
            except Exception as e:
                logger.error(f'Error during background batch reflection: {e}', exc_info=True)

    async def _synthesize_answer(self, query: str, context: list[MemoryUnit]) -> str:
        """Helper to generate an answer for opinion formation context."""

        # Simple RAG signature
        class RagSignature(dspy.Signature):
            """Answer the query given the context."""

            context = dspy.InputField()
            question = dspy.InputField()
            answer = dspy.OutputField()

        predictor = dspy.Predict(RagSignature)
        with dspy.context(lm=self.lm):
            pred = predictor(context=[u.text for u in context], question=query)
            return pred.answer

    async def reflect(self, request: ReflectionRequest) -> ReflectionResult:
        """
        Reflect on a single entity to update its Mental Model.
        """
        async with self.metastore.session() as session:
            # We instantiate ReflectionEngine per request
            from memex_core.memory.reflect.reflection import ReflectionEngine

            reflector = ReflectionEngine(session, self.config, self.embedder)

            # TODO: This engine supports batching, but API exposes single.
            models = await reflector.reflect_batch([request])
            if not models:
                # Should we raise an error? For now return empty-ish
                from memex_core.memory.sql_models import MentalModel

                return ReflectionResult(
                    entity_id=request.entity_id,
                    new_observations=[],
                    updated_model=MentalModel(
                        entity_id=request.entity_id, vault_id=request.vault_id
                    ),
                )

            mental_model = models[0]

            # Mark as complete in queue
            await self.queue_service.complete_reflection(
                session, [request.entity_id], vault_id=request.vault_id
            )

            # The MentalModel contains the new/updated observations
            return ReflectionResult(
                entity_id=request.entity_id,
                # New observations are embedded in the updated model's graph
                # or we can extract them if MentalModel tracks diffs.
                # For now, we return the full model state.
                new_observations=[Observation(**o) for o in mental_model.observations],
                updated_model=mental_model,
            )

    async def reflect_batch(self, requests: list[ReflectionRequest]) -> list[ReflectionResult]:
        """
        Reflect on multiple entities in parallel using a single DB session.
        This is significantly faster than sequential calls.
        """
        if not requests:
            return []

        async with self.metastore.session() as session:
            from memex_core.memory.reflect.reflection import ReflectionEngine

            reflector = ReflectionEngine(session, self.config, self.embedding_model)

            # reflect_batch returns list[MentalModel]
            models = await reflector.reflect_batch(requests)

            # Mark as complete in queue
            from collections import defaultdict

            processed_by_vault = defaultdict(list)
            for m in models:
                processed_by_vault[m.vault_id].append(m.entity_id)

            for vid, eids in processed_by_vault.items():
                await self.queue_service.complete_reflection(session, eids, vault_id=vid)

            results = []
            for model in models:
                results.append(
                    ReflectionResult(
                        entity_id=model.entity_id,
                        new_observations=list(model.observations),
                        updated_model=model,
                    )
                )
            return results

    async def form_opinions(self, request: OpinionFormationRequest) -> list[Any]:
        """
        Extract and persist opinions based on a recent interaction.
        """
        async with self.metastore.session() as session:
            return await self.memory.form_opinions(session, request)

    async def adjust_belief(
        self,
        unit_uuid: str | UUID,
        evidence_type_key: str,
        description: str | None = None,
    ) -> None:
        """
        Adjust the confidence of a memory unit based on new evidence.
        """
        async with self.metastore.session() as session:
            await self._extraction.adjust_belief(
                session, str(unit_uuid), evidence_type_key, description
            )
            await session.commit()

    async def create_vault(self, name: str, description: str | None = None) -> Any:
        """Create a new vault. Delegates to VaultService."""
        return await self._vaults.create_vault(name, description)

    async def delete_vault(self, vault_id: UUID) -> bool:
        """Delete a vault. Delegates to VaultService."""
        return await self._vaults.delete_vault(vault_id)

    async def delete_note(self, note_id: UUID) -> bool:
        """
        Delete a document and all associated data.

        Uses AsyncTransaction for atomicity across metastore + filestore.
        ORM cascades handle: memory_units, chunks, unit_entities, memory_links, evidence_log.
        FileStore cleanup handles: assets and filestore_path.
        """
        from memex_core.memory.sql_models import Note

        async with AsyncTransaction(self.metastore, self.filestore, str(note_id)) as txn:
            doc = await txn.db_session.get(Note, note_id)
            if not doc:
                raise NoteNotFoundError(f'Note {note_id} not found.')

            # Stage filestore deletes (deferred until commit)
            if doc.assets:
                for asset_path in doc.assets:
                    await self.filestore.delete(asset_path)
            if doc.filestore_path:
                await self.filestore.delete(doc.filestore_path, recursive=True)

            # ORM cascades handle memory_units, chunks, and their children
            await txn.db_session.delete(doc)

        return True

    async def delete_entity(self, entity_id: UUID) -> bool:
        """Delete an entity. Delegates to EntityService."""
        return await self._entities.delete_entity(entity_id)

    async def delete_mental_model(self, entity_id: UUID, vault_id: UUID) -> bool:
        """Delete a mental model. Delegates to EntityService."""
        return await self._entities.delete_mental_model(entity_id, vault_id)

    async def list_vaults(self) -> list[Any]:
        """List all vaults. Delegates to VaultService."""
        return await self._vaults.list_vaults()

    async def get_vault_by_name(self, name: str) -> Any | None:
        """Get a vault by name. Delegates to VaultService."""
        return await self._vaults.get_vault_by_name(name)

    async def get_reflection_queue_batch(
        self,
        limit: int = 10,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> list[Any]:
        """Get the next batch of items from the reflection queue."""
        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)
        async with self.metastore.session() as session:
            return await self.queue_service.get_next_batch(
                session,
                limit=limit,
                vault_ids=ids or None,
            )

    async def claim_reflection_queue_batch(
        self, limit: int = 10, vault_id: UUID | None = None
    ) -> list[Any]:
        """Claim and lock the next batch of items from the reflection queue."""
        async with self.metastore.session() as session:
            return await self.queue_service.claim_next_batch(
                session, limit=limit, vault_id=vault_id
            )

    async def get_top_entities(
        self, limit: int = 5, vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """Get top entities by mention count. Delegates to EntityService."""
        return await self._entities.get_top_entities(limit=limit, vault_ids=vault_ids)

    async def search_entities(
        self, query: str, limit: int = 10, vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """Search entities by name. Delegates to EntityService."""
        return await self._entities.search_entities(query, limit=limit, vault_ids=vault_ids)

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
