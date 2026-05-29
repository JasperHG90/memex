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
procedure keys.
"""

from __future__ import annotations

import json
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

# Re-exported from the cross-package SSOT in memex_common.kv_utils so
# this module's VALID_NAMESPACES can't drift from the SSOT.
# noqa: E402 is intentional — these imports sit mid-file (rather than at
# top) so the surrounding back-compat re-exports stay grouped with the
# explanatory comments, keeping the SSOT story readable.
from memex_common.kv_utils import VALID_NAMESPACES  # noqa: E402

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


# Procedure key detection lives in the cross-package SSOT so the HTTP
# client (memex_common) and the server (memex_core) can't drift.
# Re-exported here for back-compat with existing memex_core imports.
from memex_common.kv_utils import is_procedure_key, parse_procedure_key  # noqa: E402


def _looks_like_procedure_key(key: str) -> bool:
    """True if the key is shaped like a procedure key (valid or not).

    Used at write-time to route candidates through ``validate_procedure_key``
    so a malformed procedure-shaped key (e.g. uppercase verb) is REJECTED
    rather than silently written as a plain KV entry.

    Uses ``split`` (FIRST occurrence) — intentionally LOOSER than
    ``parse_procedure_key`` which uses ``rsplit`` (LAST occurrence). This is
    the gate; we want to catch everything that *looks* procedure-shaped and
    let ``validate_procedure_key`` make the final call. Do not "fix" this
    asymmetry without considering that the gate would then miss malformed
    keys like ``global:procedure:bad:procedure:verb:context``.
    """
    if ':procedure:' not in key:
        return False
    scope = key.split(':procedure:', 1)[0]
    return any(scope == ns or scope.startswith(f'{ns}:') for ns in VALID_NAMESPACES)


def validate_procedure_key(key: str) -> None:
    """Validate a procedure KV key.

    Raises :class:`ValueError` if the key does not match
    ``<scope>:procedure:<verb>:<context-tag>``. Valid scopes:

    - ``global`` (flat — no id segment)
    - ``user`` (flat — no id segment)
    - ``project:<id>`` (id segment required)
    - ``app:<app-id>`` (id segment required)

    ``<verb>`` and ``<context-tag>`` must match ``[a-z][a-z0-9_-]*``.
    """
    if parse_procedure_key(key) is None:
        raise ValueError(
            f'Invalid procedure key: {key!r}. '
            'Expected <scope>:procedure:<verb>:<context-tag> where scope is '
            'one of: global, user, project:<id>, or app:<app-id>; verb and '
            'context-tag must match [a-z][a-z0-9_-]*.'
        )


def format_procedure_display_name(key: str) -> str:
    """Render a procedure key for human display.

    ``global:procedure:<v>:<c>`` → ``"<v>:<c>"``.
    Any other scope → ``"[<scope>] <v>:<c>"``.
    Non-procedure key: returns the key unchanged (defensive fallback).
    """
    parsed = parse_procedure_key(key)
    if parsed is None:
        return key
    scope, verb, context = parsed
    if scope == 'global':
        return f'{verb}:{context}'
    return f'[{scope}] {verb}:{context}'


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
        # Bare `procedure:*` is a common stale form (pre-migration 046).
        # Give a targeted hint rather than the generic "must start with" message.
        if key.startswith('procedure:'):
            raise ValueError(
                f'Invalid KV key {key!r}. Bare `procedure:*` is no longer a '
                'top-level namespace — procedures live under a scope as '
                '`<scope>:procedure:<verb>:<context>`, e.g. '
                f'`global:{key}` for a global procedure.'
            )
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

        For ``procedure:`` keys the write is routed through
        :meth:`_procedure_put` which wraps ``value`` in a versioned envelope
        with capped history; for all other namespaces uses the existing
        INSERT ... ON CONFLICT DO UPDATE path.
        """
        from sqlalchemy.dialects.postgresql import insert

        from memex_core.memory.sql_models import KVEntry

        key = _normalize_key(key)
        _validate_namespace(key)

        if _looks_like_procedure_key(key):
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

        See the procedure write contract for details.
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
        ``value`` is replaced with a dict ``{value, version, history}``.
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

            if is_procedure_key(key):
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
