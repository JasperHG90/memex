"""Note service — CRUD and query operations for notes."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlmodel import col

from memex_common.exceptions import NoteNotFoundError, ResourceNotFoundError, VaultNotFoundError
from memex_common.schemas import NodeDTO

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
                metadata.setdefault('has_assets', bool(doc.assets))
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
        """Filter a TOC tree by depth and/or parent node."""
        if parent_node_id is not None:
            # Find subtree rooted at parent_node_id
            def _find_subtree(
                nodes: list[dict[str, Any]], target_id: str
            ) -> list[dict[str, Any]] | None:
                for node in nodes:
                    if node.get('id') == target_id:
                        return node.get('children', [])
                    found = _find_subtree(node.get('children', []), target_id)
                    if found is not None:
                        return found
                return None

            subtree = _find_subtree(toc, parent_node_id)
            if subtree is None:
                return []
            toc = subtree

        if depth is not None and depth >= 0:
            # depth=0 → roots + direct children (H1 + H2 overview)
            # depth=1 → full tree (no trimming)
            # depth=N (N>=1) → full tree
            effective_depth = depth + 1

            def _trim_depth(nodes: list[dict[str, Any]], current: int) -> list[dict[str, Any]]:
                if current > effective_depth:
                    return []
                result = []
                for node in nodes:
                    trimmed = dict(node)
                    trimmed['children'] = _trim_depth(node.get('children', []), current + 1)
                    result.append(trimmed)
                return result

            if depth == 0:
                toc = _trim_depth(toc, 0)
            # depth >= 1: return full tree (no trimming needed)

        return toc

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

    async def list_notes(
        self,
        limit: int = 100,
        offset: int = 0,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> list[Any]:
        """
        List ingested documents.
        Filters by the given vault_id(s), or returns all vaults if not provided.
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

            stmt = stmt.offset(offset).limit(limit)
            return list((await session.exec(stmt)).all())

    async def get_recent_notes(
        self,
        limit: int = 5,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> list[Any]:
        """Get the most recent notes."""
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

    async def delete_note(self, note_id: UUID) -> bool:
        """
        Delete a document and all associated data.

        Uses AsyncTransaction for atomicity across metastore + filestore.
        ORM cascades handle: memory_units, chunks, unit_entities, memory_links, evidence_log.
        FileStore cleanup handles: assets and filestore_path.
        After deletion, orphaned mental models (entities with no remaining links) are cleaned up.
        """
        from sqlmodel import select

        from memex_core.memory.sql_models import (
            MentalModel,
            MemoryUnit,
            Note,
            UnitEntity,
        )

        async with AsyncTransaction(self.metastore, self.filestore, str(note_id)) as txn:
            doc = await txn.db_session.get(Note, note_id)
            if not doc:
                raise NoteNotFoundError(f'Note {note_id} not found.')

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
                    await self.filestore.delete(asset_path)
            if doc.filestore_path:
                await self.filestore.delete(doc.filestore_path, recursive=True)

            # ORM cascades handle memory_units, chunks, and their children
            await txn.db_session.delete(doc)

            # Flush so cascades execute, then clean up orphaned mental models
            await txn.db_session.flush()

            if entity_ids_for_cleanup:
                for eid in entity_ids_for_cleanup:
                    # Check if any other units still reference this entity
                    remaining = await txn.db_session.exec(
                        select(UnitEntity.unit_id).where(col(UnitEntity.entity_id) == eid).limit(1)
                    )
                    if remaining.first() is None:
                        # No remaining links — delete mental models for this entity
                        mm_stmt = select(MentalModel).where(col(MentalModel.entity_id) == eid)
                        mm_result = await txn.db_session.exec(mm_stmt)
                        for mm in mm_result.all():
                            await txn.db_session.delete(mm)

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
