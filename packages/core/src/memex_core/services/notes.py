"""Note service — CRUD and query operations for notes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlmodel import col

from sqlalchemy import text

from memex_common.exceptions import NoteNotFoundError, ResourceNotFoundError, VaultNotFoundError
from memex_common.schemas import BlockSummaryDTO, NodeDTO, filter_toc

from memex_core.config import MemexConfig
from memex_core.services.vaults import VaultService
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine
from memex_core.storage.filestore import BaseAsyncFileStore
from memex_core.storage.transaction import AsyncTransaction

logger = logging.getLogger('memex.core.services.notes')


class NoteService:
    """Note CRUD, listing, and resource access."""

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        filestore: BaseAsyncFileStore,
        config: MemexConfig,
        vaults: VaultService,
    ) -> None:
        self.metastore = metastore
        self.filestore = filestore
        self.config = config
        self._vaults = vaults

    async def get_resource(self, path: str) -> bytes:
        """Direct access to stored assets in the file store."""
        return await self.filestore.load(path)

    def get_resource_path(self, path: str) -> str | None:
        """Return the absolute filesystem path for a resource, or None for remote stores."""
        from memex_core.storage.filestore import LocalAsyncFileStore

        if isinstance(self.filestore, LocalAsyncFileStore):
            return self.filestore.join_path(path)
        return None

    async def set_note_status(
        self,
        note_id: UUID,
        status: str,
        linked_note_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Set a note's lifecycle status and optionally link to another note.

        When status is 'superseded', marks all memory units as stale.
        """
        from memex_core.memory.sql_models import MemoryUnit, Note

        valid_statuses = ('active', 'superseded', 'appended')
        if status not in valid_statuses:
            raise ValueError(f'Invalid status: {status}. Must be one of {valid_statuses}.')

        async with self.metastore.session() as session:
            doc = await session.get(Note, note_id)
            if not doc:
                raise NoteNotFoundError(f'Note {note_id} not found.')

            doc.status = status
            if status == 'superseded':
                doc.superseded_by = linked_note_id
                # Cascade: mark all memory units as stale
                from sqlmodel import select

                units_stmt = select(MemoryUnit).where(col(MemoryUnit.note_id) == note_id)
                units = (await session.exec(units_stmt)).all()
                for unit in units:
                    unit.status = 'stale'
                    session.add(unit)
            elif status == 'appended':
                doc.appended_to = linked_note_id
            elif status == 'active':
                doc.superseded_by = None
                doc.appended_to = None

            session.add(doc)
            await session.commit()
            return {
                'note_id': str(note_id),
                'status': status,
                'linked_note_id': str(linked_note_id) if linked_note_id else None,
            }

    async def update_note_date(self, note_id: UUID, new_date: datetime) -> dict[str, Any]:
        """Update the publish_date of a note, cascading delta to all memory unit timestamps."""
        from datetime import timezone

        from sqlmodel import update

        from memex_core.memory.sql_models import MemoryUnit, Note

        # Normalize to UTC if naive
        if new_date.tzinfo is None:
            new_date = new_date.replace(tzinfo=timezone.utc)

        async with self.metastore.session() as session:
            doc = await session.get(Note, note_id)
            if not doc:
                raise NoteNotFoundError(f'Note {note_id} not found.')

            old_date = doc.publish_date or doc.created_at
            delta = new_date - old_date

            # Early return if no change
            if delta == timedelta(0):
                return {
                    'note_id': str(note_id),
                    'old_date': old_date.isoformat(),
                    'new_date': new_date.isoformat(),
                    'units_updated': 0,
                }

            # Update Note.publish_date
            doc.publish_date = new_date

            # Update doc_metadata
            if doc.doc_metadata is None:
                doc.doc_metadata = {}
            meta = dict(doc.doc_metadata)
            meta['publish_date'] = new_date.isoformat()
            doc.doc_metadata = meta

            # Update page_index metadata
            if isinstance(doc.page_index, dict):
                pi = dict(doc.page_index)
                pi_meta = dict(pi.get('metadata') or {})
                pi_meta['publish_date'] = new_date.isoformat()
                pi['metadata'] = pi_meta
                doc.page_index = pi

            session.add(doc)

            # Bulk update all MemoryUnit temporal fields by delta
            result = await session.exec(
                update(MemoryUnit)
                .where(col(MemoryUnit.note_id) == note_id)
                .values(
                    event_date=MemoryUnit.event_date + delta,
                    mentioned_at=MemoryUnit.mentioned_at + delta,  # type: ignore[operator]
                    occurred_start=MemoryUnit.occurred_start + delta,  # type: ignore[operator]
                    occurred_end=MemoryUnit.occurred_end + delta,  # type: ignore[operator]
                )
            )
            units_updated: int = result.rowcount  # type: ignore[assignment]

            await session.commit()
            return {
                'note_id': str(note_id),
                'old_date': old_date.isoformat(),
                'new_date': new_date.isoformat(),
                'units_updated': units_updated,
            }

    async def update_note_title(self, note_id: UUID, new_title: str) -> dict[str, Any]:
        """Update the title of a note, cascading to page_index and doc_metadata."""
        from memex_core.memory.sql_models import Note

        async with self.metastore.session() as session:
            doc = await session.get(Note, note_id)
            if not doc:
                raise NoteNotFoundError(f'Note {note_id} not found.')

            doc.title = new_title

            # Update doc_metadata
            if doc.doc_metadata is None:
                doc.doc_metadata = {}
            meta = dict(doc.doc_metadata)
            meta['name'] = new_title
            meta['title'] = new_title
            doc.doc_metadata = meta

            # Update page_index metadata
            if isinstance(doc.page_index, dict):
                pi = dict(doc.page_index)
                pi_meta = dict(pi.get('metadata') or {})
                pi_meta['title'] = new_title
                pi['metadata'] = pi_meta
                doc.page_index = pi

            session.add(doc)
            await session.commit()
            await session.refresh(doc)
            return doc.model_dump()

    async def add_note_assets(
        self,
        note_id: UUID,
        files: dict[str, bytes],
    ) -> dict[str, Any]:
        """Add one or more asset files to an existing note.

        Uses AsyncTransaction for atomicity across metastore + filestore.
        Skips files whose path already exists in the note's assets list.
        """
        from memex_core.memory.sql_models import Note, Vault

        async with AsyncTransaction(self.metastore, self.filestore, str(note_id)) as txn:
            note = await txn.db_session.get(Note, note_id)
            if not note:
                raise NoteNotFoundError(f'Note {note_id} not found.')

            vault = await txn.db_session.get(Vault, note.vault_id)
            vault_name = vault.name if vault else str(note.vault_id)

            asset_path = f'assets/{vault_name}/{note_id}'
            existing_assets = set(note.assets or [])
            added: list[str] = []
            skipped: list[str] = []

            for filename, content in files.items():
                full_asset_key = f'{asset_path}/{filename}'
                if full_asset_key in existing_assets:
                    skipped.append(filename)
                    continue
                await txn.save_file(full_asset_key, content)
                added.append(full_asset_key)

            if added:
                # New list assignment for SQLAlchemy ARRAY mutation detection
                note.assets = (note.assets or []) + added
                txn.db_session.add(note)

        return {
            'note_id': str(note_id),
            'added_assets': added,
            'skipped': skipped,
            'asset_count': len(note.assets or []),
        }

    async def delete_note_assets(
        self,
        note_id: UUID,
        asset_paths: list[str],
    ) -> dict[str, Any]:
        """Delete one or more asset files from an existing note.

        Uses AsyncTransaction for atomicity across metastore + filestore.
        Reports paths not found in the note's assets list.
        """
        from memex_core.memory.sql_models import Note

        async with AsyncTransaction(self.metastore, self.filestore, str(note_id)) as txn:
            note = await txn.db_session.get(Note, note_id)
            if not note:
                raise NoteNotFoundError(f'Note {note_id} not found.')

            existing_assets = set(note.assets or [])
            deleted: list[str] = []
            not_found: list[str] = []

            for path in asset_paths:
                if path not in existing_assets:
                    not_found.append(path)
                    continue
                await txn.delete_file(path)
                deleted.append(path)

            if deleted:
                deleted_set = set(deleted)
                note.assets = [a for a in (note.assets or []) if a not in deleted_set]
                txn.db_session.add(note)

        return {
            'note_id': str(note_id),
            'deleted_assets': deleted,
            'not_found': not_found,
            'asset_count': len(note.assets or []),
        }

    async def get_note(self, note_id: UUID) -> dict[str, Any]:
        """Retrieve a single document by ID."""
        from memex_core.memory.sql_models import Note

        async with self.metastore.session() as session:
            doc = await session.get(Note, note_id)
            if not doc:
                raise ResourceNotFoundError(f'Document {note_id} not found.')

            return doc.model_dump()

    async def get_note_metadata(self, note_id: UUID) -> dict[str, Any] | None:
        """Return just the metadata portion of the page index."""
        from memex_core.memory.sql_models import Note

        async with self.metastore.session() as session:
            doc = await session.get(Note, note_id)
            if not doc:
                raise ResourceNotFoundError(f'Document {note_id} not found.')
            if doc.page_index is None:
                return None
            if not isinstance(doc.page_index, dict):
                return None
            metadata = doc.page_index.get('metadata')
            if metadata is not None:
                from memex_core.memory.sql_models import Vault

                metadata = dict(metadata)
                metadata['has_assets'] = bool(doc.assets)
                metadata.setdefault('vault_id', str(doc.vault_id))
                vault = await session.get(Vault, doc.vault_id)
                if vault:
                    metadata.setdefault('vault_name', vault.name)
            return metadata

    @staticmethod
    def _filter_toc(
        toc: list[dict[str, Any]],
        depth: int | None = None,
        parent_node_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Filter a TOC tree by depth and/or parent node.

        Delegates to :func:`memex_common.schemas.filter_toc`.
        """
        return filter_toc(toc, depth=depth, parent_node_id=parent_node_id)

    async def get_note_page_index(self, note_id: UUID) -> dict[str, Any] | None:
        """Retrieve the page index for a document, or None if not indexed.

        Returns a dict with ``metadata`` (title, description, tags, etc.)
        and ``toc`` (the slim tree hierarchy).
        """
        from memex_core.memory.sql_models import Note

        async with self.metastore.session() as session:
            doc = await session.get(Note, note_id)
            if not doc:
                raise ResourceNotFoundError(f'Document {note_id} not found.')
            return doc.page_index

    async def get_node(self, node_id: UUID) -> NodeDTO | None:
        """Retrieve a specific document node by its ID.

        Tries a primary-key lookup first.  Falls back to querying by
        ``node_hash`` so that the MD5 content-hash IDs returned by
        ``get_note_page_index`` also resolve correctly.
        """
        from sqlmodel import select

        from memex_core.memory.sql_models import Node

        async with self.metastore.session() as session:
            node = await session.get(Node, node_id)
            if node is None:
                # Page-index IDs are MD5 content hashes stored in node_hash.
                stmt = select(Node).where(Node.node_hash == node_id.hex)
                result = await session.exec(stmt)
                node = result.first()
            if node is None:
                return None
            return NodeDTO.model_validate(node)

    async def get_nodes(self, node_ids: list[UUID]) -> list[NodeDTO]:
        """Retrieve multiple document nodes by ID.

        Tries primary-key lookup first, falls back to querying by
        ``node_hash`` so that MD5 content-hash IDs from page indexes
        also resolve correctly.
        """
        from sqlmodel import or_, select

        from memex_core.memory.sql_models import Node

        async with self.metastore.session() as session:
            hex_ids = [nid.hex for nid in node_ids]
            stmt = select(Node).where(
                or_(col(Node.id).in_(node_ids), col(Node.node_hash).in_(hex_ids))
            )
            results = (await session.exec(stmt)).all()
            return [NodeDTO.model_validate(n) for n in results]

    async def get_notes_metadata(self, note_ids: list[UUID]) -> list[dict[str, Any]]:
        """Return metadata for multiple notes in a single query.

        Skips notes that are not found or have no page_index metadata.
        """
        if not note_ids:
            return []

        from sqlmodel import select

        from memex_core.memory.sql_models import Note, Vault

        async with self.metastore.session() as session:
            stmt = select(Note).where(col(Note.id).in_(note_ids))
            notes = (await session.exec(stmt)).all()

            # Collect unique vault IDs to batch-fetch vault names
            vault_ids = {n.vault_id for n in notes if n.vault_id is not None}
            vault_map: dict[UUID, str] = {}
            if vault_ids:
                vault_stmt = select(Vault).where(col(Vault.id).in_(list(vault_ids)))
                vaults = (await session.exec(vault_stmt)).all()
                vault_map = {v.id: v.name for v in vaults}

            results: list[dict[str, Any]] = []
            for doc in notes:
                if doc.page_index is None or not isinstance(doc.page_index, dict):
                    continue
                metadata = doc.page_index.get('metadata')
                if metadata is None:
                    continue
                metadata = dict(metadata)
                metadata['has_assets'] = bool(doc.assets)
                metadata.setdefault('vault_id', str(doc.vault_id))
                vault_name = vault_map.get(doc.vault_id)
                if vault_name:
                    metadata.setdefault('vault_name', vault_name)
                metadata['note_id'] = str(doc.id)
                results.append(metadata)

            return results

    async def list_notes(
        self,
        limit: int = 100,
        offset: int = 0,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        template: str | None = None,
    ) -> list[Any]:
        """
        List ingested documents.
        Filters by the given vault_id(s), or returns all vaults if not provided.
        Optional after/before filters use COALESCE(publish_date, created_at).
        """
        from sqlalchemy import func
        from sqlmodel import select

        from memex_core.memory.sql_models import Note

        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)

        async with self.metastore.session() as session:
            stmt = select(Note)
            if ids:
                stmt = stmt.where(col(Note.vault_id).in_(ids))
            if after is not None:
                date_col = func.coalesce(Note.publish_date, Note.created_at)
                stmt = stmt.where(date_col >= after)
            if before is not None:
                date_col = func.coalesce(Note.publish_date, Note.created_at)
                stmt = stmt.where(date_col <= before)
            if template is not None:
                stmt = stmt.where(col(Note.doc_metadata)['template'].astext == template)

            stmt = stmt.order_by(Note.created_at.desc()).offset(offset).limit(limit)  # type: ignore[union-attr]
            notes = list((await session.exec(stmt)).all())
            notes = await self._attach_vault_names(session, notes)
            return await self._attach_summaries(session, notes)

    async def get_recent_notes(
        self,
        limit: int = 5,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        template: str | None = None,
    ) -> list[Any]:
        """Get the most recent notes."""
        from sqlalchemy import func
        from sqlmodel import select

        from memex_core.memory.sql_models import Note

        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)

        async with self.metastore.session() as session:
            stmt = select(Note).order_by(Note.created_at.desc())  # type: ignore[union-attr]
            if ids:
                stmt = stmt.where(col(Note.vault_id).in_(ids))
            if after is not None:
                date_col = func.coalesce(Note.publish_date, Note.created_at)
                stmt = stmt.where(date_col >= after)
            if before is not None:
                date_col = func.coalesce(Note.publish_date, Note.created_at)
                stmt = stmt.where(date_col <= before)
            if template is not None:
                stmt = stmt.where(col(Note.doc_metadata)['template'].astext == template)
            stmt = stmt.limit(limit)
            notes = list((await session.exec(stmt)).all())
            notes = await self._attach_vault_names(session, notes)
            return await self._attach_summaries(session, notes)

    @staticmethod
    async def _attach_vault_names(session: Any, notes: list[Any]) -> list[Any]:
        """Batch-fetch vault names and attach them to Note objects."""
        from sqlmodel import select

        from memex_core.memory.sql_models import Vault

        vault_ids = {n.vault_id for n in notes if n.vault_id is not None}
        vault_map: dict[UUID, str] = {}
        if vault_ids:
            vault_stmt = select(Vault).where(col(Vault.id).in_(list(vault_ids)))
            vaults = (await session.exec(vault_stmt)).all()
            vault_map = {v.id: v.name for v in vaults}
        for note in notes:
            object.__setattr__(note, 'vault_name', vault_map.get(note.vault_id))
        return notes

    @staticmethod
    async def _attach_summaries(session: Any, notes: list[Any]) -> list[Any]:
        """Batch-fetch block summaries from chunks and attach them to Note objects."""
        from sqlmodel import select

        from memex_core.memory.sql_models import Chunk

        note_ids = [n.id for n in notes]
        if not note_ids:
            return notes

        stmt = (
            select(Chunk.note_id, Chunk.summary, Chunk.chunk_index)
            .where(col(Chunk.note_id).in_(note_ids), col(Chunk.status) == 'active')
            .order_by(col(Chunk.note_id), col(Chunk.chunk_index))
        )
        result = await session.exec(stmt)
        summaries_map: dict[UUID, list[BlockSummaryDTO]] = {}
        for note_id, summary_blob, _idx in result.all():
            if summary_blob and isinstance(summary_blob, dict):
                summaries_map.setdefault(note_id, []).append(BlockSummaryDTO(**summary_blob))
        for note in notes:
            object.__setattr__(note, 'summaries', summaries_map.get(note.id, []))
        return notes

    async def find_notes_by_title(
        self,
        query: str,
        vault_ids: list[UUID] | None = None,
        limit: int = 5,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Fuzzy-search notes by title using trigram similarity.

        Uses the pg_trgm GIN index on lower(title) for efficient matching.
        Returns results ordered by similarity score descending.
        """
        async with self.metastore.session() as session:
            await session.exec(
                text('SELECT set_limit(:threshold)'), params={'threshold': threshold}
            )

            if vault_ids:
                stmt = text("""
                    SELECT
                        id, title,
                        similarity(lower(title), lower(:query)) AS score,
                        vault_id, created_at, publish_date, status
                    FROM notes
                    WHERE lower(title) % lower(:query)
                      AND vault_id = ANY(:vault_ids)
                    ORDER BY score DESC
                    LIMIT :limit
                """)
                params: dict[str, Any] = {
                    'query': query,
                    'vault_ids': list(vault_ids),
                    'limit': limit,
                }
            else:
                stmt = text("""
                    SELECT
                        id, title,
                        similarity(lower(title), lower(:query)) AS score,
                        vault_id, created_at, publish_date, status
                    FROM notes
                    WHERE lower(title) % lower(:query)
                    ORDER BY score DESC
                    LIMIT :limit
                """)
                params = {'query': query, 'limit': limit}

            result = await session.exec(stmt, params=params)
            rows = []
            for row in result:
                rows.append(
                    {
                        'note_id': row[0],
                        'title': row[1],
                        'score': float(row[2]),
                        'vault_id': row[3],
                        'created_at': row[4],
                        'publish_date': row[5],
                        'status': row[6],
                    }
                )
            return rows

    async def delete_note(self, note_id: UUID) -> bool:
        """
        Delete a document and all associated data.

        Uses AsyncTransaction for atomicity across metastore + filestore.
        ORM cascades handle: memory_units, chunks, unit_entities, memory_links, evidence_log.
        FileStore cleanup handles: assets and filestore_path.
        After deletion, orphaned entities (and their mental models) are removed, and
        mention_count is recalculated for entities still referenced by other notes.
        """
        from sqlalchemy import update
        from sqlmodel import func, select

        from memex_core.memory.sql_models import (
            Entity,
            MemoryUnit,
            MentalModel,
            Note,
            UnitEntity,
        )

        from memex_core.services.mental_model_cleanup import prune_stale_evidence

        async with AsyncTransaction(self.metastore, self.filestore, str(note_id)) as txn:
            doc = await txn.db_session.get(Note, note_id)
            if not doc:
                raise NoteNotFoundError(f'Note {note_id} not found.')

            note_vault_id = doc.vault_id

            # Collect entity_ids linked to this note's memory units before deletion.
            unit_ids_stmt = select(MemoryUnit.id).where(col(MemoryUnit.note_id) == note_id)
            unit_ids_result = await txn.db_session.exec(unit_ids_stmt)
            unit_ids = list(unit_ids_result.all())

            entity_ids_for_cleanup: set[UUID] = set()
            if unit_ids:
                entity_stmt = select(UnitEntity.entity_id).where(
                    col(UnitEntity.unit_id).in_(unit_ids)
                )
                entity_result = await txn.db_session.exec(entity_stmt)
                entity_ids_for_cleanup = set(entity_result.all())

            # Stage filestore deletes (deferred until commit)
            if doc.assets:
                for asset_path in doc.assets:
                    await txn.delete_file(asset_path)
            if doc.filestore_path:
                await txn.delete_file(doc.filestore_path, recursive=True)

            # ORM cascades handle memory_units, chunks, and their children
            await txn.db_session.delete(doc)

            # Flush so cascades execute, then clean up orphaned entities
            await txn.db_session.flush()

            orphaned_entity_ids: set[UUID] = set()
            if entity_ids_for_cleanup:
                for eid in entity_ids_for_cleanup:
                    # Check if any other units still reference this entity
                    remaining = await txn.db_session.exec(
                        select(UnitEntity.unit_id).where(col(UnitEntity.entity_id) == eid).limit(1)
                    )
                    if remaining.first() is None:
                        orphaned_entity_ids.add(eid)
                        # No remaining links — delete entity and its mental models.
                        # MentalModel has no FK CASCADE, so delete explicitly first.
                        mm_stmt = select(MentalModel).where(col(MentalModel.entity_id) == eid)
                        mm_result = await txn.db_session.exec(mm_stmt)
                        for mm in mm_result.all():
                            await txn.db_session.delete(mm)
                        # Entity FK cascades handle aliases, cooccurrences, links
                        entity = await txn.db_session.get(Entity, eid)
                        if entity:
                            await txn.db_session.delete(entity)
                    else:
                        # Update mention_count to reflect actual remaining links
                        count_result = await txn.db_session.exec(
                            select(func.count())
                            .select_from(UnitEntity)
                            .where(col(UnitEntity.entity_id) == eid)
                        )
                        actual_count = count_result.one()
                        await txn.db_session.exec(
                            update(Entity)
                            .where(col(Entity.id) == eid)
                            .values(mention_count=actual_count)
                        )

                # Prune stale evidence from mental models of shared (non-orphaned) entities
                shared_entity_ids = entity_ids_for_cleanup - orphaned_entity_ids
                if shared_entity_ids and unit_ids:
                    await prune_stale_evidence(
                        txn.db_session, shared_entity_ids, unit_ids, note_vault_id
                    )

        return True

    async def migrate_note(self, note_id: UUID, target_vault_id: UUID) -> dict[str, Any]:
        """
        Move a note and all associated data to a different vault.

        Atomically updates vault_id on the note and all child records (chunks, nodes,
        memory_units, unit_entities, memory_links), adjusts filestore paths, cleans up
        orphaned EntityCooccurrence and MentalModel rows in the source vault, then moves
        files in the filestore.
        """
        from sqlmodel import select, update

        from memex_core.memory.sql_models import (
            Chunk,
            EntityCooccurrence,
            MentalModel,
            MemoryLink,
            MemoryUnit,
            Node,
            Note,
            UnitEntity,
            Vault,
        )

        async with AsyncTransaction(self.metastore, self.filestore, str(note_id)) as txn:
            session = txn.db_session

            # Load note
            note = await session.get(Note, note_id)
            if not note:
                raise NoteNotFoundError(f'Note {note_id} not found.')

            source_vault_id = note.vault_id
            if source_vault_id == target_vault_id:
                raise ValueError('Source and target vault are the same.')

            # Validate target vault exists
            target_vault = await session.get(Vault, target_vault_id)
            if not target_vault:
                raise VaultNotFoundError(f'Target vault {target_vault_id} not found.')

            # Get source vault name for path rewriting
            source_vault = await session.get(Vault, source_vault_id)
            source_vault_name = source_vault.name if source_vault else str(source_vault_id)
            target_vault_name = target_vault.name

            # Collect unit_ids and entity_ids for this note
            unit_ids_result = await session.exec(
                select(MemoryUnit.id).where(col(MemoryUnit.note_id) == note_id)
            )
            unit_ids = list(unit_ids_result.all())

            entity_ids_for_cleanup: set[UUID] = set()
            if unit_ids:
                entity_result = await session.exec(
                    select(UnitEntity.entity_id).where(col(UnitEntity.unit_id).in_(unit_ids))
                )
                entity_ids_for_cleanup = set(entity_result.all())

            # --- Bulk UPDATE vault_id on all child tables ---

            # Note
            note.vault_id = target_vault_id

            # Rewrite filestore_path and assets
            old_prefix = f'assets/{source_vault_name}/{note_id}'
            new_prefix = f'assets/{target_vault_name}/{note_id}'
            if note.filestore_path:
                note.filestore_path = note.filestore_path.replace(old_prefix, new_prefix)
            if note.assets:
                note.assets = [a.replace(old_prefix, new_prefix) for a in note.assets]

            # Chunks
            await session.exec(
                update(Chunk).where(col(Chunk.note_id) == note_id).values(vault_id=target_vault_id)
            )

            # Nodes
            await session.exec(
                update(Node).where(col(Node.note_id) == note_id).values(vault_id=target_vault_id)
            )

            # MemoryUnits
            await session.exec(
                update(MemoryUnit)
                .where(col(MemoryUnit.note_id) == note_id)
                .values(vault_id=target_vault_id)
            )

            if unit_ids:
                # UnitEntities
                await session.exec(
                    update(UnitEntity)
                    .where(col(UnitEntity.unit_id).in_(unit_ids))
                    .values(vault_id=target_vault_id)
                )

                # MemoryLinks (from or to any of this note's units)
                await session.exec(
                    update(MemoryLink)
                    .where(col(MemoryLink.from_unit_id).in_(unit_ids))
                    .values(vault_id=target_vault_id)
                )
                await session.exec(
                    update(MemoryLink)
                    .where(col(MemoryLink.to_unit_id).in_(unit_ids))
                    .values(vault_id=target_vault_id)
                )

            # --- Cleanup EntityCooccurrence in source vault for affected entities ---
            if entity_ids_for_cleanup:
                for eid in entity_ids_for_cleanup:
                    co_stmt = select(EntityCooccurrence).where(
                        col(EntityCooccurrence.vault_id) == source_vault_id,
                        (col(EntityCooccurrence.entity_id_1) == eid)
                        | (col(EntityCooccurrence.entity_id_2) == eid),
                    )
                    co_result = await session.exec(co_stmt)
                    for co in co_result.all():
                        await session.delete(co)

            # --- Cleanup orphaned MentalModels in source vault ---
            if entity_ids_for_cleanup:
                await session.flush()
                for eid in entity_ids_for_cleanup:
                    remaining = await session.exec(
                        select(UnitEntity.unit_id)
                        .where(
                            col(UnitEntity.entity_id) == eid,
                            col(UnitEntity.vault_id) == source_vault_id,
                        )
                        .limit(1)
                    )
                    if remaining.first() is None:
                        mm_stmt = select(MentalModel).where(
                            col(MentalModel.entity_id) == eid,
                            col(MentalModel.vault_id) == source_vault_id,
                        )
                        mm_result = await session.exec(mm_stmt)
                        for mm in mm_result.all():
                            await session.delete(mm)

        # After transaction commits: move files in filestore
        if await self.filestore.exists(old_prefix):
            await self.filestore.move_file(old_prefix, new_prefix)

        return {
            'status': 'success',
            'note_id': str(note_id),
            'source_vault_id': str(source_vault_id),
            'target_vault_id': str(target_vault_id),
            'entities_affected': len(entity_ids_for_cleanup),
        }
