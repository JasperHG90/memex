from typing import TypeVar, cast, Self, Any, AsyncGenerator
import hashlib
import pathlib as plb
import logging
import asyncio
import base64
from uuid import UUID
from functools import cached_property
from datetime import datetime, timezone

from sqlalchemy import func, cast as sa_cast, Date
from sqlalchemy.types import UserDefinedType
from sqlalchemy.exc import IntegrityError

from cachetools import LRUCache, TTLCache
from cachetools_async import cached as cached_async
import dspy
from sqlmodel import col

from memex_common.exceptions import (
    VaultNotFoundError,
    AmbiguousResourceError,
    ResourceNotFoundError,
    NoteNotFoundError,
    EntityNotFoundError,
    MemoryUnitNotFoundError,
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

logger = logging.getLogger('memex.core.api')

T = TypeVar('T')

# Shared cache for vault resolution to improve performance.
# Cleared on vault deletion to ensure consistency.
_VAULT_RESOLUTION_CACHE: LRUCache = LRUCache(maxsize=32)


class JSONPath(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return 'jsonpath'


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
            self._metadata.name is None
            or self._metadata.description is None
            or self.uuid is None
            or self.etag is None
            or self._metadata.files is None
            or self._metadata.tags is None
        ):
            raise ValueError('Name and description must be set in metadata to generate manifest.')
        return (
            Manifest(
                name=self._metadata.name,
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
        except Exception:
            logger.warning(f'Could not parse frontmatter for {target_file}. Using defaults.')
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
        """Check if a vault exists in the metastore."""
        from memex_core.memory.sql_models import Vault

        async with self.metastore.session() as session:
            vault = await session.get(Vault, vault_id)
            return vault is not None

    @cached_async(cache=_VAULT_RESOLUTION_CACHE)
    async def resolve_vault_identifier(self, identifier: UUID | str) -> UUID:
        """
        Resolves a vault name or string UUID into a UUID object.
        Uses a local cache for performance.
        """
        from sqlmodel import select
        from memex_core.memory.sql_models import Vault

        parsed_uuid = None
        if isinstance(identifier, UUID):
            parsed_uuid = identifier
        else:
            try:
                parsed_uuid = UUID(str(identifier))
            except (ValueError, AttributeError):
                pass

        async with self.metastore.session() as session:
            if parsed_uuid:
                # Validate existence if it's a UUID
                vault = await session.get(Vault, parsed_uuid)
                if vault:
                    return vault.id

            # Try to resolve by name
            stmt = select(Vault).where(col(Vault.name) == str(identifier))
            vaults = (await session.exec(stmt)).all()

            if not vaults:
                raise VaultNotFoundError(
                    f"Vault '{identifier}' not found (searched by ID and Name)."
                )

            if len(vaults) > 1:
                ids = ', '.join([str(v.id) for v in vaults])
                raise AmbiguousResourceError(
                    f"Multiple vaults found with name '{identifier}': {ids}. Please use UUID."
                )

            return vaults[0].id

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
                except Exception:
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
                            except Exception:
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
        """
        Get total counts for notes, memory units, entities, and reflection queue.
        """
        from memex_core.memory.sql_models import Entity, MemoryUnit, Note, ReflectionQueue
        from sqlmodel import func, select

        # Merge single vault_id with list for backwards compat
        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)

        async with self.metastore.session() as session:
            note_stmt = select(func.count(Note.id))
            memory_stmt = select(func.count(MemoryUnit.id))
            entity_stmt = select(func.count(Entity.id))
            queue_stmt = select(func.count(ReflectionQueue.id))

            if ids:
                note_stmt = note_stmt.where(col(Note.vault_id).in_(ids))
                memory_stmt = memory_stmt.where(col(MemoryUnit.vault_id).in_(ids))
                queue_stmt = queue_stmt.where(col(ReflectionQueue.vault_id).in_(ids))

            note_count = (await session.exec(note_stmt)).one()
            memory_count = (await session.exec(memory_stmt)).one()
            entity_count = (await session.exec(entity_stmt)).one()
            queue_count = (await session.exec(queue_stmt)).one()

            return {
                'notes': note_count,
                'memories': memory_count,
                'entities': entity_count,
                'reflection_queue': queue_count,
            }

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
        """
        Stream entities ranked by hybrid score.
        Hybrid Score = 0.4 * mention_count + 0.4 * retrieval_count + 0.2 * centrality
        """
        from memex_core.memory.sql_models import Entity, EntityCooccurrence, UnitEntity
        from sqlmodel import select, func, desc, col

        # Subquery for centrality (sum of cooccurrence counts)
        # NoteInput: an entity can be either entity_id_1 or entity_id_2
        centrality_stmt = (
            select(
                func.coalesce(func.sum(EntityCooccurrence.cooccurrence_count), 0).label(
                    'centrality'
                ),
                Entity.id.label('entity_id'),
            )
            .select_from(Entity)
            .outerjoin(
                EntityCooccurrence,
                (EntityCooccurrence.entity_id_1 == Entity.id)
                | (EntityCooccurrence.entity_id_2 == Entity.id),
            )
            .group_by(Entity.id)
        ).subquery()

        stmt = select(Entity).join(centrality_stmt, centrality_stmt.c.entity_id == Entity.id)

        if vault_ids:
            stmt = (
                stmt.join(UnitEntity, col(UnitEntity.entity_id) == Entity.id)
                .where(col(UnitEntity.vault_id).in_(vault_ids))
                .distinct()
            )

        stmt = stmt.order_by(
            desc(
                0.4 * Entity.mention_count
                + 0.4 * Entity.retrieval_count
                + 0.2 * centrality_stmt.c.centrality
            )
        ).limit(limit)

        async with self.metastore.session() as session:
            stream = await session.stream(stmt)
            async for row in stream:
                yield row[0]

    async def get_entity_cooccurrences(
        self, entity_id: UUID | str, vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """
        Get co-occurrence edges for an entity.
        """
        from memex_core.memory.sql_models import EntityCooccurrence
        from sqlmodel import or_, select

        eid = UUID(str(entity_id))
        async with self.metastore.session() as session:
            stmt = select(EntityCooccurrence).where(
                or_(EntityCooccurrence.entity_id_1 == eid, EntityCooccurrence.entity_id_2 == eid)
            )
            if vault_ids:
                stmt = stmt.where(col(EntityCooccurrence.vault_id).in_(vault_ids))
            return list((await session.exec(stmt)).all())

    async def get_bulk_cooccurrences(
        self, entity_ids: list[UUID], vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """
        Get co-occurrences between a set of entities.
        """
        from memex_core.memory.sql_models import EntityCooccurrence
        from sqlmodel import col, select

        async with self.metastore.session() as session:
            stmt = select(EntityCooccurrence).where(
                (col(EntityCooccurrence.entity_id_1).in_(entity_ids))
                & (col(EntityCooccurrence.entity_id_2).in_(entity_ids))
            )
            if vault_ids:
                stmt = stmt.where(col(EntityCooccurrence.vault_id).in_(vault_ids))
            return list((await session.exec(stmt)).all())

    async def get_entity_mentions(
        self, entity_id: UUID | str, limit: int = 20, vault_ids: list[UUID] | None = None
    ) -> list[dict[str, Any]]:
        """
        Get memory units and source documents where this entity is mentioned.
        """
        from memex_core.memory.sql_models import MemoryUnit, Note, UnitEntity
        from sqlmodel import desc, select

        eid = UUID(str(entity_id))
        async with self.metastore.session() as session:
            stmt = (
                select(MemoryUnit, Note)
                .join(UnitEntity, UnitEntity.unit_id == MemoryUnit.id)
                .join(Note, MemoryUnit.note_id == Note.id)
                .where(UnitEntity.entity_id == eid)
            )
            if vault_ids:
                stmt = stmt.where(col(MemoryUnit.vault_id).in_(vault_ids))
            stmt = stmt.order_by(desc(MemoryUnit.created_at)).limit(limit)
            results = (await session.exec(stmt)).all()
            return [{'unit': unit, 'document': doc} for unit, doc in results]

    async def get_entity(self, entity_id: UUID | str) -> Any | None:
        """
        Get an entity by ID.
        """
        from memex_core.memory.sql_models import Entity

        eid = UUID(str(entity_id))
        async with self.metastore.session() as session:
            return await session.get(Entity, eid)

    async def get_memory_unit(self, unit_id: UUID | str) -> Any | None:
        """
        Get a memory unit by ID.
        """
        from memex_core.memory.sql_models import MemoryUnit

        uid = UUID(str(unit_id))
        async with self.metastore.session() as session:
            return await session.get(MemoryUnit, uid)

    async def delete_memory_unit(self, unit_id: UUID) -> bool:
        """
        Delete a memory unit and all associated data.

        ORM cascades handle: unit_entities, outgoing_links, incoming_links.
        DB FK cascade handles: evidence_log.
        """
        from memex_core.memory.sql_models import MemoryUnit

        async with self.metastore.session() as session:
            unit = await session.get(MemoryUnit, unit_id)
            if not unit:
                raise MemoryUnitNotFoundError(f'Memory unit {unit_id} not found.')

            await session.delete(unit)
            await session.commit()

        return True

    @cached_async(TTLCache(maxsize=1, ttl=300), key=lambda self: 'token_usage')
    async def get_daily_token_usage(self) -> list[dict[str, Any]]:
        """
        Get daily aggregated token usage. Cached for 5 minutes.
        """
        from memex_core.memory.sql_models import TokenUsage
        from sqlmodel import select, func

        async with self.metastore.session() as session:
            stmt = (
                select(
                    sa_cast(TokenUsage.timestamp, Date).label('date'),
                    func.sum(TokenUsage.total_tokens).label('total_tokens'),
                )
                .group_by(sa_cast(TokenUsage.timestamp, Date))
                .order_by(sa_cast(TokenUsage.timestamp, Date))
            )
            results = (await session.exec(stmt)).all()
            return [{'date': r.date, 'total_tokens': r.total_tokens} for r in results]

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
        """Create a new vault."""
        from memex_core.memory.sql_models import Vault
        from sqlmodel import select

        async with self.metastore.session() as session:
            # Check for existing vault with same name
            stmt = select(Vault).where(col(Vault.name) == name)
            existing = (await session.exec(stmt)).first()
            if existing:
                raise ValueError(f"Vault with name '{name}' already exists.")

            vault = Vault(name=name, description=description)
            session.add(vault)
            await session.commit()
            await session.refresh(vault)
            return vault

    async def delete_vault(self, vault_id: UUID) -> bool:
        """Delete a vault and its contents."""
        from memex_core.memory.sql_models import Vault

        async with self.metastore.session() as session:
            vault = await session.get(Vault, vault_id)
            if not vault:
                return False
            await session.delete(vault)
            await session.commit()
            # Clear resolution cache to prevent stale lookups
            _VAULT_RESOLUTION_CACHE.clear()
            return True

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
        """
        Delete an entity and all associated data.

        Explicit cleanup: MentalModel rows (no FK cascade exists).
        ORM cascades handle: unit_entities, aliases, memory_links, cooccurrences.
        DB FK cascade handles: reflection_queue.
        """
        from memex_core.memory.sql_models import Entity, MentalModel
        from sqlmodel import select, col

        async with self.metastore.session() as session:
            entity = await session.get(Entity, entity_id)
            if not entity:
                raise EntityNotFoundError(f'Entity {entity_id} not found.')

            # Delete MentalModel rows explicitly (no FK cascade exists)
            stmt = select(MentalModel).where(col(MentalModel.entity_id) == entity_id)
            models = (await session.exec(stmt)).all()
            for model in models:
                await session.delete(model)

            # ORM cascades handle unit_entities, aliases, memory_links, cooccurrences
            # DB FK cascade handles reflection_queue
            await session.delete(entity)
            await session.commit()

        return True

    async def delete_mental_model(self, entity_id: UUID, vault_id: UUID) -> bool:
        """
        Delete a mental model for a specific entity in a specific vault.

        Does NOT delete the parent entity.
        """
        from memex_core.memory.sql_models import MentalModel
        from sqlmodel import select, col

        async with self.metastore.session() as session:
            stmt = select(MentalModel).where(
                (col(MentalModel.entity_id) == entity_id) & (col(MentalModel.vault_id) == vault_id)
            )
            model = (await session.exec(stmt)).first()
            if not model:
                raise ResourceNotFoundError(
                    f'Mental model for entity {entity_id} in vault {vault_id} not found.'
                )

            await session.delete(model)
            await session.commit()

        return True

    async def list_vaults(self) -> list[Any]:
        """List all vaults."""
        from memex_core.memory.sql_models import Vault
        from sqlmodel import select

        async with self.metastore.session() as session:
            stmt = select(Vault)
            return list((await session.exec(stmt)).all())

    async def get_vault_by_name(self, name: str) -> Any | None:
        """Get a single vault by exact name match."""
        from memex_core.memory.sql_models import Vault
        from sqlmodel import select

        async with self.metastore.session() as session:
            stmt = select(Vault).where(Vault.name == name)
            return (await session.exec(stmt)).first()

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
        """Get top entities by mention count."""
        from memex_core.memory.sql_models import Entity, UnitEntity
        from sqlmodel import select, desc, col

        async with self.metastore.session() as session:
            stmt = select(Entity)
            if vault_ids:
                stmt = (
                    stmt.join(UnitEntity, col(UnitEntity.entity_id) == Entity.id)
                    .where(col(UnitEntity.vault_id).in_(vault_ids))
                    .distinct()
                )
            stmt = stmt.order_by(desc(Entity.mention_count)).limit(limit)
            return list((await session.exec(stmt)).all())

    async def search_entities(
        self, query: str, limit: int = 10, vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """
        Search for entities by canonical name using trigram similarity or ILIKE.
        """
        from memex_core.memory.sql_models import Entity, UnitEntity
        from sqlmodel import select, col

        async with self.metastore.session() as session:
            # Use ILIKE for broad matching
            stmt = select(Entity).where(col(Entity.canonical_name).ilike(f'%{query}%'))
            if vault_ids:
                stmt = (
                    stmt.join(UnitEntity, col(UnitEntity.entity_id) == Entity.id)
                    .where(col(UnitEntity.vault_id).in_(vault_ids))
                    .distinct()
                )
            stmt = stmt.order_by(col(Entity.mention_count).desc()).limit(limit)
            return list((await session.exec(stmt)).all())

    async def get_lineage(
        self,
        entity_type: str,
        entity_id: UUID | str,
        direction: LineageDirection = LineageDirection.UPSTREAM,
        depth: int = 3,
        limit: int = 10,
    ) -> LineageResponse:
        """
        Retrieve the full lineage (dependency chain) of a specific entity.

        Args:
            entity_type: The type of the entity (mental_model, observation, memory_unit, document).
            entity_id: The UUID of the entity.
            direction: Direction of traversal (upstream, downstream, both).
            depth: Maximum recursion depth.
            limit: Maximum number of children per node.
        """
        # Validate entity_id
        if isinstance(entity_id, str):
            entity_id = UUID(entity_id)

        async with self.metastore.session() as session:
            if direction == LineageDirection.UPSTREAM:
                return await self._get_lineage_upstream(
                    session, entity_type, entity_id, current_depth=0, max_depth=depth, limit=limit
                )
            elif direction == LineageDirection.DOWNSTREAM:
                return await self._get_lineage_downstream(
                    session, entity_type, entity_id, current_depth=0, max_depth=depth, limit=limit
                )
            else:  # BOTH
                upstream = await self._get_lineage_upstream(
                    session, entity_type, entity_id, current_depth=0, max_depth=depth, limit=limit
                )
                downstream = await self._get_lineage_downstream(
                    session, entity_type, entity_id, current_depth=0, max_depth=depth, limit=limit
                )
                # Combine children
                upstream.derived_from.extend(downstream.derived_from)
                return upstream

    def _sanitize_data(self, data: Any) -> Any:
        import numpy as np

        if isinstance(data, np.ndarray):
            return data.tolist()
        if isinstance(data, dict):
            return {k: self._sanitize_data(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._sanitize_data(i) for i in data]
        return data

    async def _get_lineage_downstream(
        self,
        session: Any,
        entity_type: str,
        entity_id: UUID,
        current_depth: int,
        max_depth: int,
        limit: int,
    ) -> LineageResponse:
        """
        Recursive helper for downstream lineage (Document -> Mental Model).
        """
        from memex_core.memory.sql_models import MentalModel, MemoryUnit, Note, Entity
        from sqlmodel import select, col

        entity_data: dict[str, Any] = {}
        children: list[LineageResponse] = []
        stop_recursion = current_depth >= max_depth

        if entity_type == 'note':
            obj = await session.get(Note, entity_id)
            if not obj:
                raise ResourceNotFoundError(f'Note {entity_id} not found.')
            entity_data = self._sanitize_data(obj.model_dump())

            if not stop_recursion:
                # Downstream: Memory Units
                stmt = select(MemoryUnit).where(col(MemoryUnit.note_id) == entity_id).limit(limit)
                units = (await session.exec(stmt)).all()
                for unit in units:
                    child_node = await self._get_lineage_downstream(
                        session, 'memory_unit', unit.id, current_depth + 1, max_depth, limit
                    )
                    children.append(child_node)

        elif entity_type == 'memory_unit':
            obj = await session.get(MemoryUnit, entity_id)
            if not obj:
                raise ResourceNotFoundError(f'Memory Unit {entity_id} not found.')
            entity_data = self._sanitize_data(obj.model_dump())

            if not stop_recursion:
                # Downstream: Observations citing this unit
                stmt = select(MentalModel).where(
                    func.jsonb_path_exists(
                        MentalModel.observations,
                        sa_cast(f'$[*].evidence[*].memory_id ? (@ == "{entity_id}")', JSONPath()),
                    )
                )
                mms = (await session.exec(stmt)).all()

                for mm in mms:
                    for obs in mm.observations:
                        evidence = obs.get('evidence', [])
                        if any(e.get('memory_id') == str(entity_id) for e in evidence):
                            obs_id = obs.get('id')
                            if obs_id:
                                try:
                                    child_node = await self._get_lineage_downstream(
                                        session,
                                        'observation',
                                        UUID(str(obs_id)),
                                        current_depth + 1,
                                        max_depth,
                                        limit,
                                    )
                                    children.append(child_node)
                                except ValueError:
                                    pass

        elif entity_type == 'observation':
            # Observation to parent Mental Model
            stmt = select(MentalModel).where(
                func.jsonb_path_exists(
                    MentalModel.observations,
                    sa_cast(f'$[*] ? (@.id == "{entity_id}")', JSONPath()),
                )
            )
            parent_mm = (await session.exec(stmt)).first()

            if not parent_mm:
                raise ResourceNotFoundError(f'Observation {entity_id} not found.')

            target_obs = None
            for obs in parent_mm.observations:
                if str(obs.get('id')) == str(entity_id):
                    target_obs = obs
                    break

            if not target_obs:
                raise ResourceNotFoundError(f'Observation {entity_id} not found.')

            entity_data = self._sanitize_data(target_obs)

            if not stop_recursion:
                # Downstream: Mental Model
                child_node = await self._get_lineage_downstream(
                    session,
                    'mental_model',
                    parent_mm.entity_id,
                    current_depth + 1,
                    max_depth,
                    limit,
                )
                children.append(child_node)

        elif entity_type == 'mental_model':
            stmt = select(MentalModel).where(
                (col(MentalModel.id) == entity_id) | (col(MentalModel.entity_id) == entity_id)
            )
            obj = (await session.exec(stmt)).first()
            if not obj:
                stmt_ent = select(Entity).where(col(Entity.id) == entity_id)
                ent = (await session.exec(stmt_ent)).first()
                if not ent:
                    raise ResourceNotFoundError(f'Entity {entity_id} not found.')
                obj = MentalModel(entity_id=entity_id, name=ent.canonical_name)

            entity_data = self._sanitize_data(obj.model_dump())

        else:
            raise ValueError(f'Unknown entity type: {entity_type}')

        return LineageResponse(entity_type=entity_type, entity=entity_data, derived_from=children)

    async def _get_lineage_upstream(
        self,
        session: Any,
        entity_type: str,
        entity_id: UUID,
        current_depth: int,
        max_depth: int,
        limit: int,
    ) -> LineageResponse:
        """
        Recursive helper for upstream lineage (Mental Model -> Document).
        """
        from memex_core.memory.sql_models import MentalModel, MemoryUnit, Note, Entity
        from sqlmodel import select, col

        # Fetch the current entity
        entity_data: dict[str, Any] = {}
        children: list[LineageResponse] = []

        # 1. Base Case: Max depth reached
        # We still fetch the entity data, but we don't recurse.
        stop_recursion = current_depth >= max_depth

        if entity_type == 'mental_model':
            # MentalModel is usually keyed by entity_id.
            stmt = select(MentalModel).where(
                (col(MentalModel.id) == entity_id) | (col(MentalModel.entity_id) == entity_id)
            )
            obj = (await session.exec(stmt)).first()
            if not obj:
                # If not found, check if it's an Entity, and return a stub Mental Model
                stmt_ent = select(Entity).where(col(Entity.id) == entity_id)
                ent = (await session.exec(stmt_ent)).first()
                if not ent:
                    raise ResourceNotFoundError(f'Entity {entity_id} not found.')
                obj = MentalModel(entity_id=entity_id, name=ent.canonical_name)

            entity_data = self._sanitize_data(obj.model_dump())

            if not stop_recursion:
                # Upstream: Observations
                # Observations are stored in the MentalModel as JSONB list.
                observations = obj.observations or []
                count = 0
                for obs in observations:
                    if count >= limit:
                        break
                    # obs is a dict
                    # We inline the observation processing since it might not have an ID (if old data)
                    # and it's more efficient than querying again.
                    obs_children = []

                    # Process evidence if we haven't reached max depth for the NEXT level (Observation -> MemoryUnit)
                    # Current depth is MentalModel. Observation is +1. MemoryUnit is +2.
                    if current_depth + 1 < max_depth:
                        evidence = obs.get('evidence', [])
                        ev_count = 0
                        for item in evidence:
                            if ev_count >= limit:
                                break
                            mem_id_str = item.get('memory_id')
                            if mem_id_str:
                                try:
                                    mem_id = UUID(str(mem_id_str))
                                    # Recurse to Memory Unit (Depth + 2)
                                    ev_child = await self._get_lineage_upstream(
                                        session,
                                        'memory_unit',
                                        mem_id,
                                        current_depth + 2,
                                        max_depth,
                                        limit,
                                    )
                                    obs_children.append(ev_child)
                                    ev_count += 1
                                except (ValueError, ResourceNotFoundError):
                                    pass

                    obs_node = LineageResponse(
                        entity_type='observation',
                        entity=self._sanitize_data(obs),
                        derived_from=obs_children,
                    )
                    children.append(obs_node)
                    count += 1

        elif entity_type == 'observation':
            stmt = select(MentalModel).where(
                func.jsonb_path_exists(
                    MentalModel.observations,
                    sa_cast(f'$[*] ? (@.id == "{entity_id}")', JSONPath()),
                )
            )
            parent_mm = (await session.exec(stmt)).first()

            if not parent_mm:
                raise ResourceNotFoundError(f'Observation {entity_id} not found.')

            # Extract the specific observation
            target_obs = None
            for obs in parent_mm.observations:
                if str(obs.get('id')) == str(entity_id):
                    target_obs = obs
                    break

            if not target_obs:
                raise ResourceNotFoundError(f'Observation {entity_id} not found in parent model.')

            entity_data = self._sanitize_data(target_obs)

            if not stop_recursion:
                # Upstream: Memory Units (from evidence)
                # evidence is a list of dicts: [{'memory_id': ...}, ...]
                evidence = target_obs.get('evidence', [])
                count = 0
                for item in evidence:
                    if count >= limit:
                        break
                    mem_id_str = item.get('memory_id')
                    if mem_id_str:
                        try:
                            mem_id = UUID(mem_id_str)
                            child_node = await self._get_lineage_upstream(
                                session, 'memory_unit', mem_id, current_depth + 1, max_depth, limit
                            )
                            children.append(child_node)
                            count += 1
                        except (ValueError, ResourceNotFoundError):
                            pass

        elif entity_type == 'memory_unit':
            obj = await session.get(MemoryUnit, entity_id)
            if not obj:
                raise ResourceNotFoundError(f'Memory Unit {entity_id} not found.')

            entity_data = self._sanitize_data(obj.model_dump())

            if not stop_recursion:
                # Upstream: Document
                if obj.note_id:
                    try:
                        child_node = await self._get_lineage_upstream(
                            session,
                            'note',
                            obj.note_id,
                            current_depth + 1,
                            max_depth,
                            limit,
                        )
                        children.append(child_node)
                    except ResourceNotFoundError:
                        pass

        elif entity_type == 'note':
            obj = await session.get(Note, entity_id)
            if not obj:
                raise ResourceNotFoundError(f'Note {entity_id} not found.')

            entity_data = self._sanitize_data(obj.model_dump())
            # Document is a leaf (upstream-wise)

        else:
            raise ValueError(f'Unknown entity type: {entity_type}')

        return LineageResponse(entity_type=entity_type, entity=entity_data, derived_from=children)
