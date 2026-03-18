"""Key-value store service — CRUD and semantic search for KV entries."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text
from sqlmodel import col, or_, select

from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.kv')

_PROTOCOL_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9+\-.]*://')

VALID_NAMESPACES = ('global', 'user', 'project')


def _normalize_key(key: str) -> str:
    """Strip well-known protocol prefixes (https://, http://, etc.) from a KV key."""
    return _PROTOCOL_RE.sub('', key)


def _validate_namespace(key: str) -> None:
    """Ensure key starts with a valid namespace prefix."""
    if not any(key.startswith(f'{ns}:') for ns in VALID_NAMESPACES):
        raise ValueError(
            f'KV key must start with a namespace prefix: '
            f'{", ".join(f"{ns}:" for ns in VALID_NAMESPACES)}'
        )


class KVService(BaseService):
    """Key-value store operations: put, get, search, delete, list."""

    async def put(
        self,
        key: str,
        value: str,
        embedding: list[float] | None = None,
    ) -> Any:
        """Upsert a KV entry. Uses INSERT ... ON CONFLICT DO UPDATE."""
        from sqlalchemy.dialects.postgresql import insert

        from memex_core.memory.sql_models import KVEntry

        key = _normalize_key(key)
        _validate_namespace(key)

        async with self.metastore.session() as session:
            stmt = insert(KVEntry).values(
                key=key,
                value=value,
                embedding=embedding,
            )
            update_set = {
                'value': stmt.excluded.value,
                'embedding': stmt.excluded.embedding,
                'updated_at': text('now()'),
            }
            stmt = stmt.on_conflict_do_update(
                constraint='uq_kv_key',
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

    async def get(self, key: str) -> Any | None:
        """Exact key lookup."""
        from memex_core.memory.sql_models import KVEntry

        key = _normalize_key(key)

        async with self.metastore.session() as session:
            stmt = select(KVEntry).where(col(KVEntry.key) == key)
            result = await session.exec(stmt)
            return result.first()

    async def search(
        self,
        query_embedding: list[float],
        namespaces: list[str] | None = None,
        limit: int = 5,
    ) -> list[Any]:
        """Semantic search over KV entries by embedding distance.

        Optionally filter by namespace prefixes.
        """
        from memex_core.memory.sql_models import KVEntry

        async with self.metastore.session() as session:
            filters: list[Any] = [col(KVEntry.embedding).is_not(None)]  # type: ignore[union-attr]
            if namespaces:
                prefix_conditions = [
                    col(KVEntry.key).startswith(f'{ns}:')  # type: ignore[union-attr]
                    for ns in namespaces
                ]
                filters.append(or_(*prefix_conditions))

            stmt = (
                select(KVEntry)
                .where(*filters)
                .order_by(KVEntry.embedding.l2_distance(query_embedding))  # type: ignore[union-attr]
                .limit(limit)
            )
            result = await session.exec(stmt)
            return list(result.all())

    async def delete(self, key: str) -> bool:
        """Delete a KV entry by key."""
        from memex_core.memory.sql_models import KVEntry

        key = _normalize_key(key)

        async with self.metastore.session() as session:
            stmt = select(KVEntry).where(col(KVEntry.key) == key)
            result = await session.exec(stmt)
            entry = result.first()
            if entry is None:
                return False
            await session.delete(entry)
            await session.commit()
            return True

    async def list_entries(
        self,
        namespaces: list[str] | None = None,
        limit: int = 100,
        exclude_prefix: str | None = None,
        key_prefix: str | None = None,
    ) -> list[Any]:
        """List KV entries, optionally filtered by namespace prefixes.

        Args:
            namespaces: Only include entries matching these namespace prefixes.
            exclude_prefix: Exclude entries whose key starts with this prefix.
            key_prefix: Only include entries whose key starts with this prefix.
        """
        from memex_core.memory.sql_models import KVEntry

        async with self.metastore.session() as session:
            stmt = select(KVEntry)
            if namespaces:
                prefix_conditions = [
                    col(KVEntry.key).startswith(f'{ns}:')  # type: ignore[union-attr]
                    for ns in namespaces
                ]
                stmt = stmt.where(or_(*prefix_conditions))
            if exclude_prefix is not None:
                stmt = stmt.where(
                    ~col(KVEntry.key).startswith(exclude_prefix)  # type: ignore[union-attr]
                )
            if key_prefix is not None:
                stmt = stmt.where(
                    col(KVEntry.key).startswith(key_prefix)  # type: ignore[union-attr]
                )
            stmt = stmt.order_by(col(KVEntry.key)).limit(limit)
            result = await session.exec(stmt)
            return list(result.all())
