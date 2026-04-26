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
from typing import TYPE_CHECKING, Any, AsyncGenerator
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from memex_core.services.notes import NoteService

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
        notes: 'NoteService | None' = None,
    ) -> None:
        self.metastore = metastore
        self.filestore = filestore
        self.config = config
        self.lm = lm
        self.memory = memory
        self._file_processor = file_processor
        self._vaults = vaults
        # Optional for backwards compatibility with callers that build the
        # IngestionService without a NoteService. append_to_note() requires it.
        self._notes = notes

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

            stmt = select(Note.content_hash, Note.original_text).where(col(Note.id) == note_uuid)
            existing_row = (await session.exec(stmt)).first()
            if existing_row is not None:
                stored_hash, stored_text = existing_row
                if stored_hash == note.content_fingerprint:
                    logger.info(f'Document {note_uuid} unchanged. Skipping ingestion.')
                    return {'status': 'skipped', 'reason': 'idempotency_check'}
                logger.info(f'Document {note_uuid} exists but content changed. Incremental update.')
                # Telemetry: track overwrites of an already-populated note. A
                # high rate is a signal that callers should switch to
                # memex_append_note for additive updates instead of resending
                # the full body.
                if stored_text:
                    try:
                        from memex_core.metrics import NOTE_RETAIN_OVERLAPS_EXISTING_TOTAL

                        NOTE_RETAIN_OVERLAPS_EXISTING_TOTAL.labels(surface='ingest_api').inc()
                    except Exception:  # pragma: no cover — metrics never block ingest
                        logger.debug('NOTE_RETAIN_OVERLAPS_EXISTING_TOTAL increment failed')

        # 3. Open Transaction
        # Staging txn_id must be unique per in-flight transaction, not derived from
        # note_uuid — two concurrent ingests of the same content would collide on
        # the filestore's _active_stages dict and clobber each other's temp paths.
        async with AsyncTransaction(self.metastore, self.filestore, str(uuid4())) as txn:
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

    async def append_to_note(
        self,
        *,
        note_id: UUID | None,
        note_key: str | None,
        vault_id: UUID | str | None,
        delta: str,
        append_id: UUID,
        joiner: str = 'paragraph',
        user_notes: str | None = None,
    ) -> dict[str, Any]:
        """Atomically append delta content onto an existing note's body.

        Identifies the parent by note_key+vault_id (preferred) or note_id, takes
        a per-parent advisory lock + SELECT FOR UPDATE, looks up append_id in
        the note_appends audit table for replay, then re-ingests the parent's
        body+delta through the existing incremental extraction pipeline. Because
        the same note_id is reused, only the new chunks invoke the LLM.

        Returns:
            dict with status ('success' | 'replayed'), note_id, append_id,
            content_hash (resulting), delta_bytes, new_unit_ids.

        Raises:
            FeatureDisabledError: server.append_enabled is False.
            NoteNotFoundError: parent not found in the supplied vault.
            NoteNotAppendableError: parent.status is archived or superseded.
            AppendIdConflictError: append_id already used with different parent/delta.
            AppendLockTimeoutError: could not acquire the append lock in time.
        """
        from memex_common.exceptions import (
            AppendIdConflictError,
            AppendLockTimeoutError,
            FeatureDisabledError,
            NoteNotAppendableError,
            NoteNotFoundError,
        )
        from memex_core.metrics import (
            NOTE_APPEND_DURATION_SECONDS,
            NOTE_APPEND_TOTAL,
        )
        import time

        _t_start = time.monotonic()
        try:
            result = await self._append_to_note_inner(
                note_id=note_id,
                note_key=note_key,
                vault_id=vault_id,
                delta=delta,
                append_id=append_id,
                joiner=joiner,
                user_notes=user_notes,
            )
            NOTE_APPEND_TOTAL.labels(outcome=result.get('status') or 'success').inc()
            return result
        except FeatureDisabledError:
            NOTE_APPEND_TOTAL.labels(outcome='disabled').inc()
            raise
        except AppendLockTimeoutError:
            NOTE_APPEND_TOTAL.labels(outcome='lock_timeout').inc()
            raise
        except AppendIdConflictError:
            NOTE_APPEND_TOTAL.labels(outcome='conflict').inc()
            raise
        except NoteNotFoundError:
            NOTE_APPEND_TOTAL.labels(outcome='not_found').inc()
            raise
        except NoteNotAppendableError:
            NOTE_APPEND_TOTAL.labels(outcome='not_appendable').inc()
            raise
        except Exception:
            NOTE_APPEND_TOTAL.labels(outcome='error').inc()
            raise
        finally:
            NOTE_APPEND_DURATION_SECONDS.observe(time.monotonic() - _t_start)

    async def _append_to_note_inner(
        self,
        *,
        note_id: UUID | None,
        note_key: str | None,
        vault_id: UUID | str | None,
        delta: str,
        append_id: UUID,
        joiner: str = 'paragraph',
        user_notes: str | None = None,
    ) -> dict[str, Any]:
        """Inner implementation of ``append_to_note``. Telemetry wraps this."""
        from memex_common.exceptions import (
            AppendIdConflictError,
            AppendLockTimeoutError,
            FeatureDisabledError,
            NoteNotAppendableError,
            NoteNotFoundError,
        )
        from memex_common.schemas import append_joiner_separator
        from memex_core.memory.sql_models import Note, NoteAppend
        from sqlalchemy import text as sa_text
        from sqlalchemy.exc import DBAPIError, OperationalError
        from sqlmodel import select
        import time

        from memex_common.schemas import (
            APPEND_DELTA_MAX_BYTES,
            APPEND_FRONTMATTER_PREFIX_PATTERN,
        )

        if not self.config.server.append_enabled:
            raise FeatureDisabledError('Atomic note-append endpoint is disabled by config.')
        if self._notes is None:
            raise RuntimeError(
                'IngestionService.append_to_note requires NoteService to be wired in.'
            )
        if not delta or not delta.strip():
            raise ValueError('delta must contain non-whitespace characters.')
        if APPEND_FRONTMATTER_PREFIX_PATTERN.match(delta):
            raise ValueError(
                "delta must not begin with '---' followed by a newline "
                '(would be ambiguous with frontmatter).'
            )
        if '\x00' in delta:
            raise ValueError('delta must not contain NUL bytes (\\x00).')
        if len(delta.encode('utf-8')) > APPEND_DELTA_MAX_BYTES:
            raise ValueError(f'delta exceeds {APPEND_DELTA_MAX_BYTES} UTF-8 bytes.')

        import unicodedata

        sep = append_joiner_separator(joiner)
        delta_encoded = delta.encode('utf-8')
        delta_bytes_count = len(delta_encoded)
        # Hash the NFC-normalised form so a retry that gets NFD-normalised by
        # an HTTP intermediary or a different agent runtime hashes the same as
        # the original call — i.e. is a true replay rather than a 409 conflict.
        # The parent body still receives the caller's original byte-form delta.
        delta_sha256 = hashlib.sha256(
            unicodedata.normalize('NFC', delta).encode('utf-8')
        ).hexdigest()
        timeout_seconds = float(self.config.server.append_lock_acquire_timeout_seconds)

        # Resolve the identifier outside the txn so a 404 short-circuits
        # before we open any locks or stage files.
        parent_id, parent_vault_id = await self._notes.resolve_note_id(
            note_id=note_id,
            note_key=note_key,
            vault_id=vault_id,
        )

        txn_id = str(uuid4())  # never reuse parent_id — would collide on _active_stages

        async with AsyncTransaction(self.metastore, self.filestore, txn_id) as txn:
            session = txn.db_session

            # Acquire per-parent advisory lock with timeout. Bigint key uses
            # the lower 63 bits of the parent UUID — collision probability is
            # negligible at any plausible vault size (~3.4B notes).
            lock_key = parent_id.int & 0x7FFFFFFFFFFFFFFF

            # SET LOCAL lock_timeout caps how long pg_advisory_xact_lock blocks.
            timeout_ms = max(1, int(timeout_seconds * 1000))
            await session.exec(sa_text(f"SET LOCAL lock_timeout = '{timeout_ms}ms'"))
            lock_acquire_start = time.monotonic()
            try:
                await session.exec(
                    sa_text('SELECT pg_advisory_xact_lock(:k)'),
                    params={'k': lock_key},
                )
            except (OperationalError, DBAPIError) as exc:
                pgcode = getattr(getattr(exc, 'orig', None), 'pgcode', None)
                if pgcode == '55P03':  # lock_not_available
                    raise AppendLockTimeoutError(
                        f'Could not acquire append lock on note {parent_id} '
                        f'within {timeout_seconds}s.'
                    ) from exc
                raise
            logger.debug(
                'append_to_note advisory lock acquired in %.3fs for note %s',
                time.monotonic() - lock_acquire_start,
                parent_id,
            )

            # Lock the parent row. lock_timeout still applies — non-append
            # writers (e.g. set_note_status, update_note_title) take the row
            # lock from a different code path; we don't want to hang here.
            try:
                parent = (
                    await session.exec(
                        select(Note).where(col(Note.id) == parent_id).with_for_update()
                    )
                ).first()
            except (OperationalError, DBAPIError) as exc:
                pgcode = getattr(getattr(exc, 'orig', None), 'pgcode', None)
                if pgcode == '55P03':  # lock_not_available — row lock contended
                    raise AppendLockTimeoutError(
                        f'Could not acquire row lock on note {parent_id} '
                        f'within {timeout_seconds}s (concurrent writer).'
                    ) from exc
                raise

            # Now that both locks are held, the extraction phase can run
            # without a deadline — drop lock_timeout to default for the rest
            # of the transaction.
            await session.exec(sa_text('SET LOCAL lock_timeout = 0'))

            # Verify state.
            if parent is None:
                raise NoteNotFoundError(f'Note {parent_id} not found.')
            if parent.status not in ('active', 'appended'):
                raise NoteNotAppendableError(
                    f'Note {parent_id} status is {parent.status!r}; '
                    f'only active or appended notes can be appended to.'
                )

            # Idempotency: did we already process this append_id?
            prior_stmt = select(NoteAppend).where(col(NoteAppend.append_id) == append_id)
            prior = (await session.exec(prior_stmt)).first()
            if prior is not None:
                if (
                    prior.note_id != parent_id
                    or prior.delta_sha256 != delta_sha256
                    or prior.joiner != joiner
                ):
                    raise AppendIdConflictError(
                        f'append_id {append_id} previously used with a different '
                        f'(note_id, delta, joiner) tuple; refusing to silently overwrite.'
                    )
                # Replay — return cached outcome verbatim. No body mutation.
                return {
                    'status': 'replayed',
                    'note_id': parent_id,
                    'append_id': append_id,
                    'content_hash': prior.resulting_content_hash,
                    'delta_bytes': prior.delta_bytes,
                    'new_unit_ids': [str(uid) for uid in prior.new_unit_ids],
                }

            # Build the new full body. We always insert sep between parent body
            # and delta when the parent has any content; if the parent is empty,
            # the delta is the whole body.
            parent_body = parent.original_text or ''
            if parent_body:
                new_body = parent_body + sep + delta
            else:
                new_body = delta

            # Carry forward the parent's metadata so the re-ingestion path
            # doesn't lose source_uri / tags / author.
            parent_meta = parent.doc_metadata or {}
            tags = list(parent_meta.get('tags') or [])
            source_uri = parent_meta.get('source_uri')
            author = parent_meta.get('author')
            template = parent_meta.get('template')
            event_date = parent.publish_date or parent.created_at

            # Build the RetainContent payload. Mirror the shape ingest() uses
            # so document tracking + chunk extraction see a familiar payload.
            retain_content = RetainContent(
                content=new_body,
                event_date=event_date,
                payload={
                    'source': 'note',
                    'note_name': parent.title,
                    'note_description': parent.description or '',
                    'author': author,
                    'uuid': str(parent_id),
                    'filestore_path': parent.filestore_path,
                    'assets': list(parent.assets or []),
                    'source_uri': source_uri,
                    # New content_hash will be computed downstream from content.
                    'content_fingerprint': hashlib.md5(new_body.encode('utf-8')).hexdigest(),
                    'tags': tags,
                    'template': template,
                },
                vault_id=parent.vault_id,
            )

            # Re-ingest with the SAME note_id. Existing two-gate idempotency
            # sees the note exists (gate-1) but content_hash differs (gate-2)
            # → routes to the incremental extraction path, which only invokes
            # the LLM on the new chunks introduced by `delta`.
            result = await self.memory.retain(
                session=session,
                contents=[retain_content],
                note_id=str(parent_id),
                reflect_after=False,
                agent_name='append',
            )
            new_unit_ids: list[UUID] = [UUID(str(uid)) for uid in result.get('unit_ids') or []]

            # If the caller supplied user_notes, stash them on the metadata
            # without re-injecting into the body (the parent already had its
            # user_notes baked into original_text on first ingest).
            if user_notes is not None:
                from sqlalchemy.orm.attributes import flag_modified

                refreshed = (
                    await session.exec(select(Note).where(col(Note.id) == parent_id))
                ).first()
                if refreshed is not None:
                    md = dict(refreshed.doc_metadata or {})
                    md['user_notes'] = user_notes
                    refreshed.doc_metadata = md
                    flag_modified(refreshed, 'doc_metadata')
                    session.add(refreshed)

            # Audit row — same transaction as the body mutation, so rollback
            # erases both. After commit, the row is the canonical record for
            # idempotent replay.
            #
            # The new content_hash was just upserted by track_document; query
            # it back so the audit reflects what's actually persisted (we
            # don't want to re-implement track_document's hash logic here).
            refreshed_hash = (
                await session.exec(select(Note.content_hash).where(col(Note.id) == parent_id))
            ).first()
            # ``applied_at`` is filled by the server-default (now()) at INSERT
            # time, so the audit timestamp reflects actual commit time rather
            # than start-of-call (lock-acquire latency would otherwise skew it).
            audit_row = NoteAppend(
                append_id=append_id,
                note_id=parent_id,
                delta_sha256=delta_sha256,
                delta_bytes=delta_bytes_count,
                joiner=joiner,
                resulting_content_hash=refreshed_hash or '',
                new_unit_ids=new_unit_ids,
            )
            session.add(audit_row)

        # ─── Post-commit work ──────────────────────────────────────────────
        # audit_event spawns a background task immediately; firing it from
        # inside the AsyncTransaction would let the audit log record an
        # `note.appended` even when the body mutation rolled back. Emit it
        # only after the `async with` exits cleanly.
        audit_event(
            self._audit_service,
            'note.appended',
            'note',
            str(parent_id),
            append_id=str(append_id),
            delta_bytes=delta_bytes_count,
        )

        # Contradictions can run on committed data.
        contradiction_coro = result.pop('contradiction_task', None)
        if contradiction_coro is not None:
            try:
                await contradiction_coro
            except Exception:
                logger.exception(
                    'Post-commit contradiction detection failed for appended note %s',
                    parent_id,
                )

        return {
            'status': 'success',
            'note_id': parent_id,
            'append_id': append_id,
            'content_hash': refreshed_hash or '',
            'delta_bytes': delta_bytes_count,
            'new_unit_ids': [str(uid) for uid in new_unit_ids],
        }

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

        # Staging txn_id must be unique per in-flight transaction; deriving it from
        # the note UUID (the idempotency key) causes concurrent ingests of the same
        # content to collide on the filestore's _active_stages dict.
        chunk_txn_id = uuid4()
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
