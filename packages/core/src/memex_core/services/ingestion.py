"""Ingestion service — note ingestion from URLs, files, and raw content."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import pathlib as plb
import re
import tempfile
from datetime import date, datetime, timezone
from typing import Any, AsyncGenerator
from uuid import UUID

import dspy
import stamina
import yaml
from dateutil import parser as dateutil_parser
from sqlmodel import col

from memex_core.config import MemexConfig
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.extraction.models import RetainContent
from memex_core.processing.dates import extract_document_date
from memex_core.processing.files import FileContentProcessor
from memex_core.processing.models import ExtractedContent
from memex_core.processing.titles import (
    _is_meaningful_name,
    extract_title_via_llm,
    resolve_document_title,
)
from memex_core.processing.web import WebContentProcessor
from memex_core.services.audit import AuditService, audit_event
from memex_core.services.vaults import VaultService
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.storage.transaction import AsyncTransaction

logger = logging.getLogger('memex.core.services.ingestion')

FRONTMATTER_PATTERN = re.compile(r'\A---\s*\n(?P<yaml>.*?)\n---\s*\n', re.DOTALL)
DATE_FIELD_NAMES = (
    'date',
    'publish_date',
    'published_at',
    'created_date',
    'created',
    'published',
)


def _parse_frontmatter_date(val: Any) -> datetime | None:
    """Parse a frontmatter date value into a timezone-aware UTC datetime."""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
    if isinstance(val, str):
        try:
            dt = dateutil_parser.parse(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, OverflowError):
            return None
    return None


def _extract_date_from_frontmatter(content: str) -> datetime | None:
    """Parse YAML frontmatter for date fields. Returns timezone-aware datetime or None."""
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return None
    try:
        fm = yaml.safe_load(match.group('yaml'))
    except Exception:
        return None
    if not isinstance(fm, dict):
        return None
    for field in DATE_FIELD_NAMES:
        val = fm.get(field)
        if val is not None:
            parsed = _parse_frontmatter_date(val)
            if parsed is not None:
                return parsed
    return None


# Extensions that FileContentProcessor can handle (non-markdown).
_CONVERTIBLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        '.pdf',
        '.docx',
        '.xlsx',
        '.pptx',
        '.csv',
        '.json',
        '.xml',
        '.html',
        '.htm',
        '.msg',
        '.eml',
    }
)


def _needs_conversion(dto: Any) -> bool:
    """Check if a NoteCreateDTO requires file-format conversion.

    Returns True when the DTO has a filename whose extension is not
    markdown and is in the set of convertible formats.
    """
    filename = getattr(dto, 'filename', None)
    if not filename:
        return False
    suffix = plb.Path(filename).suffix.lower()
    return suffix in _CONVERTIBLE_EXTENSIONS


async def _convert_to_markdown(
    raw_bytes: bytes,
    filename: str,
    file_processor: FileContentProcessor,
) -> ExtractedContent:
    """Convert binary file content to Markdown via FileContentProcessor.

    Writes raw_bytes to a temp file with the correct suffix so the
    processor can dispatch on extension. Returns the ExtractedContent
    with .content (markdown str) and .images (dict[str, bytes]).
    """
    suffix = plb.Path(filename).suffix
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, raw_bytes)
        os.close(fd)
        return await file_processor.extract(plb.Path(tmp_path))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _wrap_extracted_content(extracted: ExtractedContent, filename: str) -> str:
    """Wrap FileContentProcessor output in YAML frontmatter (matches ingest_from_file pattern)."""
    now = datetime.now(timezone.utc).isoformat()
    extra_fm = ''
    if extracted.metadata.get('author'):
        extra_fm += f'\nauthor: {extracted.metadata["author"]}'
    if extracted.metadata.get('creation_date'):
        extra_fm += f'\ncreated_date: {extracted.metadata["creation_date"].isoformat()}'
    return (
        f'---\nsource_file: {filename}\ntype: {extracted.content_type}'
        f'{extra_fm}\ningested_at: {now}\n---\n{extracted.content}\n'
    )


_RETRYABLE_PG_CODES = frozenset(
    {
        '40P01',  # deadlock_detected
        '57014',  # query_canceled (statement timeout)
        '40001',  # serialization_failure
    }
)


def _is_retryable_db_error(exc: Exception) -> bool:
    """Check if an exception is a transient PostgreSQL error worth retrying."""
    from sqlalchemy.exc import DBAPIError, OperationalError

    if isinstance(exc, (OperationalError, DBAPIError)):
        pgcode = getattr(getattr(exc, 'orig', None), 'pgcode', None)
        return pgcode in _RETRYABLE_PG_CODES
    return False


class IngestionService:
    """Note ingestion from URLs, files, and raw NoteInput objects."""

    _audit_service: AuditService | None = None

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        filestore: BaseAsyncFileStore,
        config: MemexConfig,
        lm: dspy.LM,
        memory: MemoryEngine,
        file_processor: FileContentProcessor,
        vaults: VaultService,
    ) -> None:
        self.metastore = metastore
        self.filestore = filestore
        self.config = config
        self.lm = lm
        self.memory = memory
        self._file_processor = file_processor
        self._vaults = vaults

    async def ingest_from_url(
        self,
        url: str,
        vault_id: UUID | str | None = None,
        reflect_after: bool = True,
        assets: dict[str, bytes] | None = None,
        user_notes: str | None = None,
    ) -> dict[str, Any]:
        """Ingest content from a URL and store it as a NoteInput."""
        from memex_core.api import NoteInput

        try:
            extracted = await WebContentProcessor.fetch_and_extract(url)
        except ValueError as e:
            logger.error(f'Failed to fetch {url}: {e}')
            raise

        target_vault_id = await self._vaults.resolve_vault_identifier(
            vault_id or self.config.server.default_active_vault
        )

        title = extracted.metadata.get('title') or None
        now = datetime.now(timezone.utc).isoformat()

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
            user_notes=user_notes,
        )

        # Resolve document date: web metadata -> LLM fallback -> now()
        event_date = extracted.document_date
        if event_date is None:
            event_date = await extract_document_date(extracted.content, self.lm)

        result = await self.ingest(note, vault_id=target_vault_id, event_date=event_date)
        audit_event(
            self._audit_service,
            'note.ingested_url',
            'note',
            str(result.get('note_id', '')),
            url=url,
        )
        return result

    async def ingest_from_file(
        self,
        file_path: str | plb.Path,
        vault_id: UUID | str | None = None,
        reflect_after: bool = True,
        note_key: str | None = None,
        user_notes: str | None = None,
    ) -> dict[str, Any]:
        """
        Ingest content from a path.
        - If it's a directory or a .md file, it's treated as a native NoteInput.
        - Otherwise, it uses MarkItDown for extraction.
        """
        from memex_core.api import NoteInput

        path = plb.Path(file_path)

        if path.is_dir() or path.suffix.lower() == '.md':
            target_vault_id = await self._vaults.resolve_vault_identifier(
                vault_id or self.config.server.default_active_vault
            )
            logger.info(f'Ingesting {path} as a native NoteInput.')
            note = await NoteInput.from_file(path, user_notes=user_notes)
            return await self.ingest(note, vault_id=target_vault_id)

        try:
            extracted = await self._file_processor.extract(path)
        except Exception as e:
            logger.error(f'Failed to extract {path}: {e}')
            raise

        target_vault_id = await self._vaults.resolve_vault_identifier(
            vault_id or self.config.server.default_active_vault
        )

        now = datetime.now(timezone.utc).isoformat()

        extra_fm = ''
        if extracted.metadata.get('author'):
            extra_fm += f'\nauthor: {extracted.metadata["author"]}'
        if extracted.metadata.get('creation_date'):
            extra_fm += f'\ncreated_date: {extracted.metadata["creation_date"].isoformat()}'

        note_content = f"""---
source_file: {path.name}
type: {extracted.content_type}{extra_fm}
ingested_at: {now}
---
{extracted.content}
"""

        original_hash = hashlib.md5(extracted.content.encode('utf-8')).hexdigest()

        name = extracted.metadata.get('title') or path.stem

        # For file imports, prefer LLM title extraction over H1 regex
        # when PDF metadata title is missing (path.stem is often meaningless).
        if not _is_meaningful_name(name):
            llm_title = await extract_title_via_llm(
                extracted.content[:1500],
                self.lm,
            )
            if llm_title:
                name = llm_title

        note = NoteInput(
            name=name,
            description=f'Content from {path.name}',
            content=note_content.encode('utf-8'),
            tags=['file-import'],
            source_uri=str(path.absolute()),
            original_content_hash=original_hash,
            files=extracted.images,
            note_key=note_key,
            user_notes=user_notes,
        )

        # Resolve document date priority:
        # 1. LLM content extraction (always attempted)
        # 2. PDF metadata creation date
        # 3. File processor's document_date (mtime)
        # 4. Final fallback to now()
        event_date = await extract_document_date(extracted.content, self.lm)

        # 2. PDF metadata creation date
        if event_date is None:
            event_date = extracted.metadata.get('creation_date')

        # 3. File processor's document_date (mtime)
        if event_date is None:
            event_date = extracted.document_date

        # 4. Final fallback — avoids duplicate LLM call inside ingest()
        if event_date is None:
            event_date = datetime.now(timezone.utc)

        result = await self.ingest(note, vault_id=target_vault_id, event_date=event_date)
        audit_event(
            self._audit_service,
            'note.ingested_file',
            'note',
            str(result.get('note_id', '')),
            file_path=str(path),
        )
        return result

    async def ingest(
        self,
        note: Any,
        vault_id: UUID | str | None = None,
        event_date: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Transactional ingestion of a note into Memex.

        Workflow:
        1. Calculate ID (NoteInput.idempotency_key).
        2. Idempotency Check: Skip if exists in MetaStore.
        3. Transaction: Open AsyncTransaction.
        4. Stage Files: Save to FileStore.
        5. Extract Facts: Run MemoryEngine.retain in DB session.
        6. Commit: 2PC via AsyncTransaction.
        """
        note_uuid = note.idempotency_key
        logger.info(f'Ingesting note: {note._metadata.name} (UUID: {note_uuid})')

        # Determine Target Vault
        target_vault_id = await self._vaults.resolve_vault_identifier(
            vault_id or self.config.server.default_active_vault
        )

        # 2. Two-Gate Idempotency Check
        async with self.metastore.session() as session:
            from memex_core.memory.sql_models import Vault, Note
            from sqlmodel import select

            vault = await session.get(Vault, target_vault_id)
            vault_name = vault.name if vault else str(target_vault_id)

            stmt = select(Note.content_hash).where(col(Note.id) == note_uuid)
            stored_hash = (await session.exec(stmt)).first()
            if stored_hash is not None:
                if stored_hash == note.content_fingerprint:
                    logger.info(f'Document {note_uuid} unchanged. Skipping ingestion.')
                    return {'status': 'skipped', 'reason': 'idempotency_check'}
                logger.info(f'Document {note_uuid} exists but content changed. Incremental update.')

        # 3. Open Transaction
        async with AsyncTransaction(self.metastore, self.filestore, note_uuid) as txn:
            # 4. Stage Files (FS)
            asset_path = f'assets/{vault_name}/{note_uuid}'
            asset_files_list = []

            for filename, content in note._files.items():
                full_asset_key = f'{asset_path}/{filename}'
                await txn.save_file(full_asset_key, content)
                asset_files_list.append(full_asset_key)

            # 5. Extract Facts (MS)
            content_text = note._content.decode('utf-8')

            resolved_title = await resolve_document_title(
                content_text,
                note._metadata.name,
                self.lm,
            )

            # Resolve event_date: passed value -> frontmatter -> LLM -> now()
            if event_date is None:
                event_date = _extract_date_from_frontmatter(content_text)
            if event_date is None:
                event_date = await extract_document_date(content_text, self.lm)
            if event_date is None:
                event_date = datetime.now(timezone.utc)

            retain_content = RetainContent(
                content=content_text,
                event_date=event_date,
                payload={
                    'source': 'note',
                    'note_name': resolved_title,
                    'note_description': note._metadata.description,
                    'author': note._metadata.author,
                    'uuid': note_uuid,
                    'filestore_path': asset_path if asset_files_list else None,
                    'assets': asset_files_list,
                    'source_uri': note.source_uri,
                    'content_fingerprint': note.content_fingerprint,
                    'tags': note._metadata.tags or [],
                    'template': note.template,
                },
                vault_id=target_vault_id,
            )

            result = await self.memory.retain(
                session=txn.db_session,
                contents=[retain_content],
                note_id=note_uuid,
                reflect_after=False,
                agent_name='user',
            )
            result['note_id'] = note_uuid
            result['status'] = 'success'

            # Overlap detection: find similar existing notes via chunk embeddings
            overlapping = await self._detect_overlapping_notes(
                txn.db_session, note_uuid, target_vault_id
            )
            if overlapping:
                result['overlapping_notes'] = overlapping

            audit_event(
                self._audit_service,
                'note.ingested',
                'note',
                str(note_uuid),
                title=resolved_title,
            )

        # Transaction committed — safe to run contradiction (new session sees committed data).
        contradiction_coro = result.pop('contradiction_task', None)
        if contradiction_coro is not None:
            try:
                await contradiction_coro
            except Exception:
                logger.exception(
                    'Post-commit contradiction detection failed for note %s', note_uuid
                )

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

        Yields cumulative progress dicts after each *chunk* (batch_size group of
        notes), not after each individual note.  This is intentional: each chunk
        is processed inside a single DB transaction via ``_process_chunk``, so
        yielding finer-grained updates would require breaking transactional
        atomicity.  Consumers (e.g. ``BatchManager``) use the cumulative
        ``processed_count / skipped_count / failed_count`` totals to report
        progress.
        """
        from memex_core.api import NoteInput
        from memex_core.memory.sql_models import Vault, Note
        from sqlmodel import select

        target_vault_id = await self._vaults.resolve_vault_identifier(
            vault_id or self.config.server.default_active_vault
        )

        # 1. Resolve Vault Name for path organization
        async with self.metastore.session() as session:
            vault = await session.get(Vault, target_vault_id)
            vault_name = vault.name if vault else str(target_vault_id)

        # 2. Two-Gate Idempotency Check
        note_uuids = [UUID(NoteInput.calculate_idempotency_key_from_dto(n)) for n in notes]
        note_fingerprints = [NoteInput.calculate_fingerprint_from_dto(n) for n in notes]
        async with self.metastore.session() as session:
            stmt = select(Note.id, Note.content_hash).where(col(Note.id).in_(note_uuids))
            db_result = await session.exec(stmt)
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
                    results['skipped_count'] += 1
                    continue
            to_process.append((i, note_dto, note_uuid))

        # Initial yield for skipped items
        yield results

        # 3. Batch Processing Loop
        for i in range(0, len(to_process), batch_size):
            chunk = to_process[i : i + batch_size]

            try:
                processed_ids = await self._process_chunk(
                    chunk=chunk,
                    vault_name=vault_name,
                    note_fingerprints=note_fingerprints,
                    target_vault_id=target_vault_id,
                )
                results['processed_count'] += len(processed_ids)
                results['note_ids'].extend(processed_ids)
                yield results

            except Exception as e:
                logger.error('Failed to process ingestion chunk: %s', e, exc_info=True)
                results['failed_count'] += len(chunk)
                results['errors'].append({'chunk_start': i, 'error': str(e)})
                yield results

    @stamina.retry(
        on=_is_retryable_db_error,
        attempts=3,
        timeout=None,
        wait_initial=1.0,
        wait_max=4.0,
    )
    async def _process_chunk(
        self,
        chunk: list[tuple[int, Any, UUID]],
        vault_name: str,
        note_fingerprints: list[str],
        target_vault_id: UUID,
    ) -> list[str]:
        """Process a single chunk of notes within a transaction.

        Returns a list of processed note ID strings.
        Retries automatically on transient PostgreSQL errors (deadlocks,
        statement timeouts, serialization failures).
        """
        from memex_core.api import inject_user_notes

        chunk_txn_id = chunk[0][2]
        processed_ids: list[str] = []
        _pending_contradictions: list = []

        async with AsyncTransaction(self.metastore, self.filestore, str(chunk_txn_id)) as txn:
            for original_idx, note_dto, note_uuid in chunk:
                asset_path = f'assets/{vault_name}/{note_uuid}'
                asset_files_list = []

                # --- Format conversion for non-markdown content ---
                if _needs_conversion(note_dto):
                    raw_bytes = note_dto.content_decoded
                    extracted = await _convert_to_markdown(
                        raw_bytes,
                        note_dto.filename,
                        self._file_processor,
                    )
                    decoded_content = _wrap_extracted_content(
                        extracted,
                        note_dto.filename,
                    )
                    extracted_images = extracted.images
                else:
                    decoded_content = note_dto.content_decoded.decode('utf-8')
                    extracted_images = {}

                decoded_content = inject_user_notes(
                    decoded_content, getattr(note_dto, 'user_notes', None)
                )

                for filename, content in note_dto.files.items():
                    try:
                        raw_content = base64.b64decode(content)
                    except Exception as e:
                        logger.debug(
                            'Base64 decode failed for file %r, using raw: %s',
                            filename,
                            e,
                        )
                        raw_content = content

                    full_asset_key = f'{asset_path}/{filename}'
                    await txn.save_file(full_asset_key, raw_content)
                    asset_files_list.append(full_asset_key)

                # Stage extracted images (from PDF conversion, etc.)
                for img_name, img_bytes in extracted_images.items():
                    full_asset_key = f'{asset_path}/{img_name}'
                    await txn.save_file(full_asset_key, img_bytes)
                    asset_files_list.append(full_asset_key)

                resolved_title = await resolve_document_title(
                    decoded_content,
                    note_dto.name,
                    self.lm,
                )

                # Extract date from frontmatter, fall back to now()
                batch_event_date = _extract_date_from_frontmatter(decoded_content)
                if batch_event_date is None:
                    batch_event_date = datetime.now(timezone.utc)

                retain_content = RetainContent(
                    content=decoded_content,
                    event_date=batch_event_date,
                    payload={
                        'source': 'batch_note',
                        'note_name': resolved_title,
                        'note_description': note_dto.description,
                        'author': getattr(note_dto, 'author', None),
                        'uuid': str(note_uuid),
                        'filestore_path': asset_path if asset_files_list else None,
                        'assets': asset_files_list,
                        'content_fingerprint': note_fingerprints[original_idx],
                        'tags': note_dto.tags or [],
                    },
                    vault_id=target_vault_id,
                )

                retain_result = await self.memory.retain(
                    session=txn.db_session,
                    contents=[retain_content],
                    note_id=str(note_uuid),
                    reflect_after=False,
                    agent_name='user',
                )
                _coro = retain_result.pop('contradiction_task', None)
                if _coro is not None:
                    _pending_contradictions.append(_coro)

                processed_ids.append(str(note_uuid))

        # Transaction committed — safe to run contradiction detection.
        for coro in _pending_contradictions:
            try:
                await coro
            except Exception:
                logger.exception('Post-commit contradiction detection failed in batch chunk')

        return processed_ids

    async def _detect_overlapping_notes(
        self,
        session: Any,
        note_id: UUID,
        vault_id: UUID,
        similarity_threshold: float = 0.85,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Find existing notes with high chunk-level similarity to the newly ingested note."""
        from sqlalchemy import text as sql_text

        # Use raw SQL for pgvector cosine distance aggregation
        query = sql_text("""
            WITH new_chunks AS (
                SELECT embedding
                FROM chunks
                WHERE note_id = :note_id AND embedding IS NOT NULL
            ),
            similarities AS (
                SELECT
                    c.note_id,
                    AVG(1 - (c.embedding <=> nc.embedding)) AS avg_similarity
                FROM chunks c
                CROSS JOIN new_chunks nc
                WHERE c.note_id != :note_id
                  AND c.vault_id = :vault_id
                  AND c.status = 'active'
                  AND c.embedding IS NOT NULL
                GROUP BY c.note_id
                HAVING AVG(1 - (c.embedding <=> nc.embedding)) >= :threshold
                ORDER BY avg_similarity DESC
                LIMIT :max_results
            )
            SELECT s.note_id, s.avg_similarity, n.title
            FROM similarities s
            JOIN notes n ON n.id = s.note_id
        """)

        try:
            result = await session.exec(
                query,
                params={
                    'note_id': str(note_id),
                    'vault_id': str(vault_id),
                    'threshold': similarity_threshold,
                    'max_results': max_results,
                },
            )
            rows = result.all()
            return [
                {
                    'note_id': str(row[0]),
                    'similarity': round(float(row[1]), 4),
                    'title': row[2],
                }
                for row in rows
            ]
        except Exception as e:
            logger.warning(f'Overlap detection failed (non-fatal): {e}')
            return []
