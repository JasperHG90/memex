"""Key-value store service — CRUD and semantic search for KV entries.

KV namespaces
-------------

Keys must start with one of the valid namespace prefixes:

- ``global:`` — vault-default operational state
- ``user:`` — per-user preferences
- ``project:`` — per-project bindings
- ``app:`` — per-application configuration
- ``procedure:`` — procedural observations

The ``procedure:<verb>:<context-tag>`` namespace is special: writes use
last-writer-wins on the active value with a ``version`` increment, and
superseded values are appended to a capped (5-entry) history kept inside
the same KV row's JSON envelope (NO schema change to ``kv_entries``).
The agent owns the procedure (the verb — ``write_pr``, ``run_tests``);
Memex stores observations about how to ADAPT it to specific contexts
(the context-tag — ``commit-style``, ``python-monorepo``).

See :func:`validate_procedure_key` for the strict format check on
procedure keys (RFC-007).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlmodel import col, or_, select

from memex_core.services.audit import audit_event
from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.kv')

_PROTOCOL_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9+\-.]*://')

VALID_NAMESPACES = ('global', 'user', 'project', 'app', 'procedure')

PROCEDURE_KEY_RE = re.compile(r'^procedure:[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*$')

PROCEDURE_HISTORY_CAP = 5
PROCEDURE_RETRY_BUDGET = 5


class ProcedureKVConcurrencyError(RuntimeError):
    """Raised when procedure-key optimistic concurrency exhausts its retry budget."""

    def __init__(self, key: str, retries: int) -> None:
        super().__init__(
            f'Procedure KV write contention: {retries} retries exhausted on key {key!r}.'
        )
        self.key = key
        self.retries = retries


def validate_procedure_key(key: str) -> None:
    """Validate a ``procedure:<verb>:<context-tag>`` KV key.

    Raises :class:`ValueError` if the key does not match the strict
    namespace format. The regex only permits lowercase letters, digits,
    hyphens, and underscores in segments, with a leading lowercase letter
    on each of the verb and context-tag segments. Exactly two colons.

    See RFC-007 §53-61 for the contract.
    """
    if not PROCEDURE_KEY_RE.match(key):
        raise ValueError(
            f'Invalid procedure key: {key!r}. '
            'Expected procedure:<verb>:<context-tag> with each segment '
            'matching [a-z][a-z0-9_-]*.'
        )


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
        """Upsert a KV entry.

        For ``procedure:`` keys (RFC-007) the write is routed through
        :meth:`_procedure_put` which wraps ``value`` in a versioned envelope
        with capped history; for all other namespaces uses the existing
        INSERT ... ON CONFLICT DO UPDATE path.
        """
        from sqlalchemy.dialects.postgresql import insert

        from memex_core.memory.sql_models import KVEntry

        key = _normalize_key(key)
        _validate_namespace(key)

        if key.startswith('procedure:'):
            validate_procedure_key(key)
            return await self._procedure_put(key, value, embedding=embedding)

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

    async def _procedure_put(
        self,
        key: str,
        value: str,
        embedding: list[float] | None = None,
    ) -> Any:
        """Write a procedure-key value with version + capped history.

        Single-row optimistic-concurrency UPDATE on the JSON envelope; on
        rowcount=0 (concurrent writer beat us) re-reads and retries up to
        :data:`PROCEDURE_RETRY_BUDGET` times. First write creates the
        envelope at v=1 with empty history. Each subsequent write
        increments the version, appends the previously-active value to
        history, and caps history at :data:`PROCEDURE_HISTORY_CAP` entries
        (oldest dropped).

        See RFC-007 §63-112 for the contract.
        """
        from sqlalchemy.dialects.postgresql import insert

        from memex_core.memory.sql_models import KVEntry

        for _ in range(PROCEDURE_RETRY_BUDGET):
            async with self.metastore.session() as session:
                existing_stmt = select(KVEntry).where(col(KVEntry.key) == key)
                result = await session.exec(existing_stmt)
                existing = result.first()

                if existing is None:
                    # First write — create envelope at v=1, empty history.
                    new_payload = {
                        'v': 1,
                        'value': value,
                        'tags': {},
                        'history': [],
                    }
                    stmt = insert(KVEntry).values(
                        key=key,
                        value=json.dumps(new_payload),
                        embedding=embedding,
                    )
                    stmt = stmt.on_conflict_do_nothing(constraint='uq_kv_key')
                    stmt = stmt.returning(KVEntry.__table__)
                    insert_result = await session.execute(stmt)
                    row = insert_result.first()
                    await session.commit()
                    if row is not None:
                        entry = await session.get(KVEntry, row.id)
                        audit_event(
                            self._audit_service,
                            'kv.procedure_written',
                            'kv',
                            key,
                            version=1,
                        )
                        return entry
                    # Lost the race to a concurrent INSERT; retry with UPDATE path.
                    continue

                try:
                    parsed = json.loads(existing.value)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError(
                        f'Procedure KV envelope at {key!r} is not valid JSON: {exc}'
                    ) from exc

                old_v = int(parsed['v'])
                new_v = old_v + 1
                superseded = {
                    'v': old_v,
                    'value': parsed['value'],
                    'superseded_at': datetime.now(timezone.utc).isoformat(),
                }
                new_history = (parsed.get('history') or []) + [superseded]
                new_history = new_history[-PROCEDURE_HISTORY_CAP:]
                new_payload = {
                    'v': new_v,
                    'value': value,
                    'tags': parsed.get('tags') or {},
                    'history': new_history,
                }

                update_stmt = text(
                    'UPDATE kv_entries SET value = :v, updated_at = now() '
                    "WHERE key = :k AND (value::jsonb->>'v') = :expected"
                )
                update_result = await session.execute(
                    update_stmt,
                    {
                        'v': json.dumps(new_payload),
                        'k': key,
                        'expected': str(old_v),
                    },
                )
                if update_result.rowcount == 1:
                    await session.commit()
                    refresh_stmt = select(KVEntry).where(col(KVEntry.key) == key)
                    refresh_result = await session.exec(refresh_stmt)
                    entry = refresh_result.first()
                    audit_event(
                        self._audit_service,
                        'kv.procedure_written',
                        'kv',
                        key,
                        version=new_v,
                    )
                    return entry
                # Concurrent writer beat us; retry.
                await session.rollback()

        raise ProcedureKVConcurrencyError(key, PROCEDURE_RETRY_BUDGET)

    async def get(
        self,
        key: str,
        *,
        include_history: bool = False,
    ) -> Any | None:
        """Exact key lookup. Expired entries are deleted on read.

        For ``procedure:`` keys: by default returns an entry whose ``value``
        is the unwrapped active value (back-compat — non-procedure callers
        and existing procedure-naive callers see a string, not the JSON
        envelope). With ``include_history=True``, the returned entry's
        ``value`` is replaced with a dict ``{value, version, history}`` per
        RFC-007 §114-116.
        """
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

            if key.startswith('procedure:'):
                try:
                    parsed = json.loads(entry.value)
                except (ValueError, TypeError):
                    # Fallback: not an envelope (legacy data); return as-is.
                    return entry
                if include_history:
                    entry.value = {  # type: ignore[assignment]
                        'value': parsed['value'],
                        'version': parsed['v'],
                        'history': parsed.get('history') or [],
                    }
                else:
                    entry.value = parsed['value']

            return entry

    async def list_top_procedure_outcomes(
        self,
        vault_id: str | UUID,
        *,
        context: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Top procedure outcomes for a vault, ranked by Memory Worth.

        Returns up to ``limit`` rows from ``procedure_outcomes`` joined to
        ``kv_entries`` for the given ``vault_id``, ordered by
        ``compute_mw_score(success_co_count, failure_co_count) DESC`` and
        tie-broken by ``last_outcome_at DESC NULLS LAST``. The MW score is
        computed in-database with the SAME Beta-Bernoulli formula as
        :func:`memex_core.services.outcomes.compute_mw_score`
        (``(s + 1) / (s + f + 2)``).

        If ``context`` is provided, narrow results to procedure keys whose
        context-tag contains it (substring match on the third
        ``procedure:<verb>:<context-tag>`` segment).
        """
        if limit <= 0:
            return []
        if not isinstance(vault_id, UUID):
            try:
                vault_uuid: UUID = UUID(str(vault_id))
            except ValueError as exc:
                raise ValueError(f'Invalid vault_id: {vault_id}') from exc
        else:
            vault_uuid = vault_id

        # MW score formula MUST match compute_mw_score: (s+1)/(s+f+2).
        sql = (
            'SELECT '
            '  po.kv_key, '
            '  po.success_co_count, '
            '  po.failure_co_count, '
            '  ((po.success_co_count + 1)::float / '
            '   (po.success_co_count + po.failure_co_count + 2)::float) AS mw_score, '
            '  po.last_outcome_at '
            'FROM procedure_outcomes po '
            'JOIN kv_entries kv ON kv.key = po.kv_key '
            'WHERE po.vault_id = :vid '
        )
        params: dict[str, Any] = {'vid': vault_uuid, 'lim': limit}
        if context:
            # context-tag is the segment after the last colon. Escape ILIKE
            # metacharacters in the user-supplied value so '%' / '_' are
            # treated literally; the ESCAPE clause uses '\' as the escape
            # character (doubled in the Python string for SQL literal '\\').
            escaped_context = context.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            sql += "AND split_part(po.kv_key, ':', 3) ILIKE :ctx ESCAPE '\\' "
            params['ctx'] = f'%{escaped_context}%'
        sql += 'ORDER BY mw_score DESC, po.last_outcome_at DESC NULLS LAST, po.kv_key LIMIT :lim'

        async with self.metastore.session() as session:
            result = await session.execute(text(sql), params)
            rows = result.all()

        return [
            {
                'kv_key': r[0],
                'success_co_count': int(r[1]),
                'failure_co_count': int(r[2]),
                'mw_score': float(r[3]),
                'last_outcome_at': r[4],
            }
            for r in rows
        ]

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
