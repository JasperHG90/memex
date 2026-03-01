"""Note service — CRUD and query operations for notes."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlmodel import col

from memex_common.exceptions import NoteNotFoundError, ResourceNotFoundError
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

    async def get_note(self, note_id: UUID) -> dict[str, Any]:
        """Retrieve a single document by ID."""
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
        Filters by the given vault_id(s), or the active vault if not provided.
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
                target_vault_id = await self._vaults.resolve_vault_identifier(
                    self.config.server.active_vault
                )
                stmt = stmt.where(col(Note.vault_id) == target_vault_id)

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
