"""Key-value store service — CRUD and semantic search for KV entries."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import col, or_, select

from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.kv')


class KVService(BaseService):
    """Key-value store operations: put, get, search, delete, list."""

    async def put(
        self,
        vault_id: UUID | None,
        key: str,
        value: str,
        embedding: list[float] | None = None,
    ) -> Any:
        """Upsert a KV entry. Uses INSERT ... ON CONFLICT DO UPDATE."""
        from sqlalchemy.dialects.postgresql import insert

        from memex_core.memory.sql_models import KVEntry

        async with self.metastore.session() as session:
            stmt = insert(KVEntry).values(
                vault_id=vault_id,
                key=key,
                value=value,
                embedding=embedding,
            )
            update_set = {
                'value': stmt.excluded.value,
                'embedding': stmt.excluded.embedding,
                'updated_at': text('now()'),
            }
            if vault_id is not None:
                stmt = stmt.on_conflict_do_update(
                    constraint='uq_kv_vault_key',
                    set_=update_set,
                )
            else:
                # NULL vault_id: PostgreSQL treats NULLs as distinct in regular
                # UNIQUE constraints, so target the partial unique index instead.
                stmt = stmt.on_conflict_do_update(
                    index_elements=['key'],
                    index_where=text('vault_id IS NULL'),
                    set_=update_set,
                )
            stmt = stmt.returning(KVEntry.__table__)
            result = await session.exec(stmt)  # type: ignore[arg-type]
            row = result.first()
            await session.commit()

            if row is None:
                raise RuntimeError('Upsert returned no row')

            # Fetch the full ORM object to return
            entry = await session.get(KVEntry, row.id)
            return entry

    async def get(self, key: str, vault_id: UUID | None = None) -> Any | None:
        """Exact key lookup. If vault_id given, check vault-specific first, fallback to global."""
        from memex_core.memory.sql_models import KVEntry

        async with self.metastore.session() as session:
            if vault_id is not None:
                # Try vault-specific first
                stmt = select(KVEntry).where(
                    col(KVEntry.key) == key,
                    col(KVEntry.vault_id) == vault_id,
                )
                result = await session.exec(stmt)
                entry = result.first()
                if entry is not None:
                    return entry

            # Fallback to global (vault_id IS NULL)
            stmt = select(KVEntry).where(
                col(KVEntry.key) == key,
                col(KVEntry.vault_id).is_(None),  # type: ignore[union-attr]
            )
            result = await session.exec(stmt)
            return result.first()

    async def search(
        self,
        query_embedding: list[float],
        vault_id: UUID | None = None,
        limit: int = 5,
    ) -> list[Any]:
        """Semantic search over KV entries by embedding distance.

        Includes both vault-scoped and global entries.
        """
        from memex_core.memory.sql_models import KVEntry

        async with self.metastore.session() as session:
            # Build filter: include global + vault-scoped entries
            filters = [col(KVEntry.embedding).is_not(None)]  # type: ignore[union-attr]
            if vault_id is not None:
                filters.append(
                    or_(
                        col(KVEntry.vault_id) == vault_id,
                        col(KVEntry.vault_id).is_(None),  # type: ignore[union-attr]
                    )
                )
            else:
                filters.append(col(KVEntry.vault_id).is_(None))  # type: ignore[union-attr]

            stmt = (
                select(KVEntry)
                .where(*filters)
                .order_by(KVEntry.embedding.l2_distance(query_embedding))  # type: ignore[union-attr]
                .limit(limit)
            )
            result = await session.exec(stmt)
            return list(result.all())

    async def delete(self, key: str, vault_id: UUID | None = None) -> bool:
        """Delete a KV entry by key and optional vault scope."""
        from memex_core.memory.sql_models import KVEntry

        async with self.metastore.session() as session:
            if vault_id is not None:
                stmt = select(KVEntry).where(
                    col(KVEntry.key) == key,
                    col(KVEntry.vault_id) == vault_id,
                )
            else:
                stmt = select(KVEntry).where(
                    col(KVEntry.key) == key,
                    col(KVEntry.vault_id).is_(None),  # type: ignore[union-attr]
                )
            result = await session.exec(stmt)
            entry = result.first()
            if entry is None:
                return False
            await session.delete(entry)
            await session.commit()
            return True

    async def list_entries(self, vault_id: UUID | None = None, limit: int = 100) -> list[Any]:
        """List KV entries. No vault_id = global only; with vault_id = both vault-scoped + global."""
        from memex_core.memory.sql_models import KVEntry

        async with self.metastore.session() as session:
            if vault_id is not None:
                stmt = select(KVEntry).where(
                    or_(
                        col(KVEntry.vault_id) == vault_id,
                        col(KVEntry.vault_id).is_(None),  # type: ignore[union-attr]
                    )
                )
            else:
                stmt = select(KVEntry).where(
                    col(KVEntry.vault_id).is_(None)  # type: ignore[union-attr]
                )
            stmt = stmt.order_by(col(KVEntry.key)).limit(limit)
            result = await session.exec(stmt)
            return list(result.all())
