"""Vault service — CRUD and resolution for Memex vaults."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from cachetools import LRUCache
from cachetools_async import cached as cached_async
from sqlmodel import col

from memex_common.exceptions import VaultNotFoundError, AmbiguousResourceError

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
            return True

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
