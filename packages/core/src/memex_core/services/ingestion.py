"""Ingestion service — note ingestion from URLs, files, and raw content."""

from __future__ import annotations

import base64
import hashlib
import logging
import pathlib as plb
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from uuid import UUID

import dspy
from sqlmodel import col

from memex_core.config import MemexConfig
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.extraction.models import RetainContent
from memex_core.processing.dates import extract_document_date
from memex_core.processing.files import FileContentProcessor
from memex_core.processing.titles import resolve_document_title
from memex_core.processing.web import WebContentProcessor
from memex_core.services.vaults import VaultService
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.storage.transaction import AsyncTransaction

logger = logging.getLogger('memex.core.services.ingestion')


class IngestionService:
    """Note ingestion from URLs, files, and raw NoteInput objects."""

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
    ) -> dict[str, Any]:
        """Ingest content from a URL and store it as a NoteInput."""
        from memex_core.api import NoteInput

        try:
            extracted = await WebContentProcessor.fetch_and_extract(url)
        except ValueError as e:
            logger.error(f'Failed to fetch {url}: {e}')
            raise

        target_vault_id = await self._vaults.resolve_vault_identifier(
            vault_id or self.config.server.active_vault
        )

        title = extracted.metadata.get('title') or None
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
        - If it's a directory or a .md file, it's treated as a native NoteInput.
        - Otherwise, it uses MarkItDown for extraction.
        """
        from memex_core.api import NoteInput

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

        target_vault_id = await self._vaults.resolve_vault_identifier(
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
        note: Any,
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
        """
        note_uuid = note.uuid
        logger.info(f'Ingesting note: {note._metadata.name} (UUID: {note_uuid})')

        # Determine Target Vault
        target_vault_id = await self._vaults.resolve_vault_identifier(
            vault_id or self.config.server.active_vault
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
                    'filestore_path': asset_path if asset_files_list else None,
                    'assets': asset_files_list,
                    'source_uri': note.source_uri,
                    'content_fingerprint': note.content_fingerprint,
                    'tags': note._metadata.tags or [],
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
        """
        from memex_core.api import NoteInput
        from memex_core.memory.sql_models import Vault, Note
        from sqlmodel import select

        target_vault_id = await self._vaults.resolve_vault_identifier(
            vault_id or self.config.server.active_vault
        )

        # 1. Resolve Vault Name for path organization
        async with self.metastore.session() as session:
            vault = await session.get(Vault, target_vault_id)
            vault_name = vault.name if vault else str(target_vault_id)

        # 2. Two-Gate Idempotency Check
        note_uuids = [UUID(NoteInput.calculate_uuid_from_dto(n)) for n in notes]
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
                chunk_txn_id = chunk[0][2]

                async with AsyncTransaction(self.metastore, self.filestore, chunk_txn_id) as txn:
                    chunk_doc_ids = []

                    for original_idx, note_dto, note_uuid in chunk:
                        asset_path = f'assets/{vault_name}/{note_uuid}'
                        asset_files_list = []

                        decoded_content = note_dto.content_decoded.decode('utf-8')

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

                        resolved_title = await resolve_document_title(
                            decoded_content,
                            note_dto.name,
                            self.lm,
                            session=txn.db_session,
                            vault_id=target_vault_id,
                        )

                        retain_content = RetainContent(
                            content=decoded_content,
                            event_date=datetime.now(timezone.utc),
                            payload={
                                'source': 'batch_note',
                                'note_name': resolved_title,
                                'note_description': note_dto.description,
                                'uuid': str(note_uuid),
                                'filestore_path': asset_path if asset_files_list else None,
                                'assets': asset_files_list,
                                'content_fingerprint': note_fingerprints[original_idx],
                                'tags': note_dto.tags or [],
                            },
                            vault_id=target_vault_id,
                        )

                        await self.memory.retain(
                            session=txn.db_session,
                            contents=[retain_content],
                            note_id=str(note_uuid),
                            reflect_after=False,
                            agent_name='user',
                        )
                        chunk_doc_ids.append(note_uuid)

                    results['processed_count'] += len(chunk)
                    results['note_ids'].extend([str(uid) for uid in chunk_doc_ids])

            except Exception as e:
                logger.error(f'Failed to process ingestion chunk: {e}', exc_info=True)
                results['failed_count'] += len(chunk)
                results['errors'].append({'chunk_start': i, 'error': str(e)})

            yield results

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
