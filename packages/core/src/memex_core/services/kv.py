"""Key-value store service — CRUD and semantic search for KV entries."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlmodel import col, or_, select

from memex_core.services.audit import audit_event
from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.kv')

_PROTOCOL_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9+\-.]*://')

VALID_NAMESPACES = ('global', 'user', 'project', 'app')


def _pattern_to_prefix(pattern: str) -> str | None:
    """Convert a trailing-wildcard pattern to a key prefix."""
    if pattern == '*':
        return None
    if '*' in pattern and not pattern.endswith('*'):
        raise ValueError('Only trailing wildcards are supported (e.g. "global:preferences:*")')
    return pattern.rstrip('*')


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


def _not_expired_filter() -> Any:
    """SQL filter that excludes expired KV entries."""
    from memex_core.memory.sql_models import KVEntry

    return or_(
        col(KVEntry.expires_at).is_(None),  # type: ignore[union-attr]
        col(KVEntry.expires_at) > text('now()'),  # type: ignore[union-attr]
    )


class KVService(BaseService):
    """Key-value store operations: put, get, search, delete, list."""

    async def put(
        self,
        key: str,
        value: str,
        embedding: list[float] | None = None,
        ttl_seconds: int | None = None,
    ) -> Any:
        """Upsert a KV entry. Uses INSERT ... ON CONFLICT DO UPDATE."""
        from sqlalchemy.dialects.postgresql import insert

        from memex_core.memory.sql_models import KVEntry

        key = _normalize_key(key)
        _validate_namespace(key)

        expires_at_val: datetime | None = None
        if ttl_seconds is not None:
            if ttl_seconds <= 0:
                raise ValueError('ttl_seconds must be a positive integer')
            expires_at_val = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        async with self.metastore.session() as session:
            stmt = insert(KVEntry).values(
                key=key,
                value=value,
                embedding=embedding,
                expires_at=expires_at_val,
            )
            update_set = {
                'value': stmt.excluded.value,
                'embedding': stmt.excluded.embedding,
                'expires_at': stmt.excluded.expires_at,
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
            audit_event(self._audit_service, 'kv.written', 'kv', key)
            return entry

    async def get(self, key: str) -> Any | None:
        """Exact key lookup. Expired entries are deleted on read."""
        from memex_core.memory.sql_models import KVEntry

        key = _normalize_key(key)

        async with self.metastore.session() as session:
            stmt = select(KVEntry).where(col(KVEntry.key) == key)
            result = await session.exec(stmt)
            entry = result.first()

            if entry is None:
                return None

            if entry.expires_at is not None and entry.expires_at <= datetime.now(timezone.utc):
                await session.delete(entry)
                await session.commit()
                return None

            return entry

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
            filters: list[Any] = [
                col(KVEntry.embedding).is_not(None),  # type: ignore[union-attr]
                _not_expired_filter(),
            ]
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
            audit_event(self._audit_service, 'kv.deleted', 'kv', key)
            return True

    async def list_entries(
        self,
        namespaces: list[str] | None = None,
        limit: int = 100,
        exclude_prefix: str | None = None,
        key_prefix: str | None = None,
        pattern: str | None = None,
    ) -> list[Any]:
        """List KV entries, optionally filtered by namespace prefixes.

        Args:
            namespaces: Only include entries matching these namespace prefixes.
            exclude_prefix: Exclude entries whose key starts with this prefix.
            key_prefix: Only include entries whose key starts with this prefix.
            pattern: Wildcard filter (e.g. "global:preferences:*"). Only trailing * supported.
        """
        if pattern is not None:
            if key_prefix is not None:
                raise ValueError('Cannot specify both pattern and key_prefix')
            key_prefix = _pattern_to_prefix(pattern)

        from memex_core.memory.sql_models import KVEntry

        async with self.metastore.session() as session:
            stmt = select(KVEntry).where(_not_expired_filter())
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

    async def cleanup_expired(self) -> int:
        """Delete all expired KV entries. Returns the count of deleted rows."""
        async with self.metastore.session() as session:
            stmt = text(
                'DELETE FROM kv_entries WHERE expires_at IS NOT NULL AND expires_at <= now()'
            )
            result = await session.exec(stmt)  # type: ignore[arg-type]
            await session.commit()
            return result.rowcount  # type: ignore[union-attr]
