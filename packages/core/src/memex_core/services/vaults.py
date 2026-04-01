"""Vault service — CRUD and resolution for Memex vaults."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from cachetools import LRUCache
from cachetools_async import cached as cached_async
from sqlmodel import col

from memex_common.exceptions import VaultNotFoundError, AmbiguousResourceError

from memex_core.services.audit import audit_event
from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.vaults')

# Shared cache for vault resolution to improve performance.
# Cleared on vault deletion to ensure consistency.
_VAULT_RESOLUTION_CACHE: LRUCache = LRUCache(maxsize=32)


class VaultService(BaseService):
    """Vault CRUD operations and identifier resolution."""

    async def validate_vault_exists(self, vault_id: UUID) -> bool:
        """Check if a vault exists in the metastore."""
        from memex_core.memory.sql_models import Vault

        async with self.metastore.session() as session:
            vault = await session.get(Vault, vault_id)
            return vault is not None

    @cached_async(cache=_VAULT_RESOLUTION_CACHE)
    async def resolve_vault_identifier(self, identifier: UUID | str) -> UUID:
        """Resolves a vault name or string UUID into a UUID object.

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
                vault = await session.get(Vault, parsed_uuid)
                if vault:
                    return vault.id

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

    async def create_vault(self, name: str, description: str | None = None) -> Any:
        """Create a new vault."""
        from memex_core.memory.sql_models import Vault
        from sqlmodel import select

        async with self.metastore.session() as session:
            stmt = select(Vault).where(col(Vault.name) == name)
            existing = (await session.exec(stmt)).first()
            if existing:
                raise ValueError(f"Vault with name '{name}' already exists.")

            vault = Vault(name=name, description=description)
            session.add(vault)
            await session.commit()
            await session.refresh(vault)
            audit_event(self._audit_service, 'vault.created', 'vault', str(vault.id), name=name)
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
            _VAULT_RESOLUTION_CACHE.clear()
            audit_event(self._audit_service, 'vault.deleted', 'vault', str(vault_id))
            return True

    async def truncate_vault(self, vault_id: UUID) -> dict[str, int]:
        """Remove all content from a vault without deleting the vault itself.

        Entities are shared across vaults — they are only deleted if they have
        no mental models remaining in *any* vault after this vault's mental
        models are removed.

        Returns a dict of table names to the number of rows deleted.
        """
        from sqlmodel import delete, select, col

        from memex_core.memory.sql_models import (
            BatchJob,
            Entity,
            MentalModel,
            MemoryUnit,
            Note,
            ReflectionQueue,
        )

        counts: dict[str, int] = {}
        filestore_paths: list[str] = []

        async with self.metastore.session() as session:
            # 0. Collect filestore paths from notes before deletion
            note_stmt = select(Note.filestore_path, Note.assets).where(
                col(Note.vault_id) == vault_id
            )
            note_rows = (await session.exec(note_stmt)).all()
            for filestore_path, assets in note_rows:
                if filestore_path:
                    filestore_paths.append(filestore_path)
                if assets:
                    filestore_paths.extend(assets)

            # 1. Find entities that will become orphaned after we delete
            #    this vault's mental models. An entity is orphaned if the
            #    only mental models it has are in this vault.
            orphan_subq = (
                select(MentalModel.entity_id)
                .where(col(MentalModel.vault_id) == vault_id)
                .where(
                    ~MentalModel.entity_id.in_(  # type: ignore[union-attr]
                        select(MentalModel.entity_id).where(col(MentalModel.vault_id) != vault_id)
                    )
                )
            )
            orphan_entity_ids = list((await session.exec(orphan_subq)).all())

            # 2. Delete vault-scoped tables (children before parents).
            #    KVEntry and AuditLog are excluded — they are not vault-scoped.
            vault_tables = [
                ('reflection_queue', ReflectionQueue),
                ('mental_models', MentalModel),
                ('memory_units', MemoryUnit),
                ('notes', Note),
                ('batch_jobs', BatchJob),
            ]
            for label, model in vault_tables:
                stmt = delete(model).where(col(model.vault_id) == vault_id)  # type: ignore[attr-defined]
                result = await session.exec(stmt)  # type: ignore[arg-type]
                counts[label] = result.rowcount  # type: ignore[union-attr]

            # 3. Delete orphaned entities (no mental models in any vault)
            if orphan_entity_ids:
                stmt_ent = delete(Entity).where(col(Entity.id).in_(orphan_entity_ids))
                result_ent = await session.exec(stmt_ent)  # type: ignore[arg-type]
                counts['entities'] = result_ent.rowcount  # type: ignore[union-attr]
            else:
                counts['entities'] = 0

            await session.commit()

        # 4. Clean up filestore files (best-effort — DB records are already gone)
        for path in filestore_paths:
            try:
                await self.filestore.delete(path, recursive=True)
            except Exception:
                logger.warning('Failed to delete filestore path during vault truncate: %s', path)

        audit_event(self._audit_service, 'vault.truncated', 'vault', str(vault_id))
        return counts

    async def list_vaults(self) -> list[Any]:
        """List all vaults."""
        from memex_core.memory.sql_models import Vault
        from sqlmodel import select

        async with self.metastore.session() as session:
            stmt = select(Vault)
            return list((await session.exec(stmt)).all())

    async def list_vaults_with_counts(self) -> list[dict[str, Any]]:
        """List all vaults with note counts and last-modified timestamp."""
        from sqlalchemy import func
        from sqlmodel import select
        from memex_core.memory.sql_models import Vault, Note

        async with self.metastore.session() as session:
            stmt = (
                select(
                    Vault,
                    func.count(Note.id).label('note_count'),
                    func.max(Note.created_at).label('last_note_added_at'),
                )
                .outerjoin(Note, Note.vault_id == Vault.id)
                .group_by(Vault.id)
                .order_by(
                    func.max(Note.created_at).desc().nulls_last(),
                    func.count(Note.id).desc(),
                )
            )
            results = (await session.exec(stmt)).all()
            return [
                {
                    'vault': row[0],
                    'note_count': row[1],
                    'last_note_added_at': row[2],
                }
                for row in results
            ]

    async def get_vault_by_name(self, name: str) -> Any | None:
        """Get a single vault by exact name match."""
        from memex_core.memory.sql_models import Vault
        from sqlmodel import select

        async with self.metastore.session() as session:
            stmt = select(Vault).where(Vault.name == name)
            return (await session.exec(stmt)).first()
