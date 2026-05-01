"""Per-entity Postgres advisory-lock helpers + LocksService (F9).

Single-int advisory locks keyed off entity UUID. The high bit (1 << 62) places
all entity lock ids in [2^62, 2^63-1] — disjoint by construction from the
leader lock at ~2^52. See RFC-005.

Acquire/release pattern uses a dedicated short-lived asyncpg connection per
context-manager invocation. Lock release is deterministic on context exit; if
the connection dies mid-hold (process crash, network failure), Postgres
auto-releases the lock when the backend terminates. Both behaviours validated
in POC-F9 (commit bba8094, merged at 866ec74).

LocksService orchestrates `memex_memory_reconsolidate` (entity-scoped) and
`memex_memory_consolidate` (vault-wide). RFC-005 / RFC-008.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, NamedTuple
from uuid import UUID

import asyncpg
from sqlalchemy.engine.url import make_url
from sqlmodel import select

from memex_common.exceptions import MemexError

if TYPE_CHECKING:
    from memex_core.config import MemexConfig
    from memex_core.memory.contradiction.engine import ContradictionEngine
    from memex_core.services.reflection import ReflectionService
    from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.locks')

LEADER_LOCK_ID: Final[int] = 5432789123456789
ENTITY_LOCK_HIGH_BIT: Final[int] = 1 << 62
ENTITY_LOCK_MASK: Final[int] = (1 << 62) - 1


class EntityLockTimeoutError(MemexError):
    """Raised when a per-entity advisory lock could not be acquired in time.

    Indicates concurrent reconsolidation on the same entity. Surface to the
    user as 'another reconsolidation is in progress; retry in a moment'.
    """

    pass


class LockIdSplit(NamedTuple):
    """Pair of (classid, objid) shown in pg_locks for single-int advisory locks.

    objsubid is always 1 for single-int advisory locks (and 2 for two-int);
    not part of this split because it's a constant for our scheme.
    """

    classid: int
    objid: int


def entity_lock_id(entity_id: uuid.UUID) -> int:
    """Derive a deterministic int64 advisory lock id from an entity UUID.

    The high bit is set so the result lives in [2^62, 2^63-1], disjoint from
    LEADER_LOCK_ID (~2^52). Deterministic by construction — the same UUID
    yields the same lock_id across processes (no PYTHONHASHSEED dependency).
    """
    raw = int.from_bytes(entity_id.bytes, 'big', signed=False) & ENTITY_LOCK_MASK
    return ENTITY_LOCK_HIGH_BIT | raw


def split_for_pg_locks(lock_id: int) -> LockIdSplit:
    """Split a single-int advisory lock id into the (classid, objid) pair
    Postgres exposes in pg_locks.

    Empirically verified against pg18-trixie: classid is the high 32 bits,
    objid is the low 32 bits, objsubid is always 1 for single-int locks.
    """
    classid = (lock_id >> 32) & 0xFFFFFFFF
    objid = lock_id & 0xFFFFFFFF
    return LockIdSplit(classid=classid, objid=objid)


_RETRY_INTERVAL_SECONDS: Final[float] = 0.1


@asynccontextmanager
async def acquire_entity_lock(
    dsn: str,
    entity_id: uuid.UUID,
    *,
    timeout_seconds: float = 30.0,
) -> AsyncIterator[None]:
    """Acquire a per-entity Postgres advisory lock for the duration of the
    context.

    Uses a dedicated short-lived asyncpg connection so the lock survives the
    SQLAlchemy session lifecycle of any work performed inside the body
    (validated in POC-F9 / RFC-005). Spins on `pg_try_advisory_lock` with a
    bounded wait — blocking `pg_advisory_lock` would hang an MCP request
    indefinitely if the lock is contended.

    Raises EntityLockTimeoutError when timeout_seconds elapses without
    acquiring. On context exit (success OR exception), the lock is released
    and the connection closed; if the process crashes before exit, Postgres
    auto-releases on backend termination (POC TC-12-5).
    """
    lock_id = entity_lock_id(entity_id)
    conn = await asyncpg.connect(dsn)
    acquired = False
    try:
        deadline = monotonic() + timeout_seconds
        while True:
            got = await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
            if got:
                acquired = True
                break
            if monotonic() >= deadline:
                raise EntityLockTimeoutError(
                    f'could not acquire advisory lock for entity {entity_id} '
                    f'within {timeout_seconds}s (lock_id={lock_id})'
                )
            await asyncio.sleep(_RETRY_INTERVAL_SECONDS)
        yield
    finally:
        if acquired:
            try:
                await conn.execute('SELECT pg_advisory_unlock($1)', lock_id)
            except Exception:
                logger.exception(
                    'pg_advisory_unlock failed for entity %s; relying on '
                    'connection close to release',
                    entity_id,
                )
        await conn.close()


_DEFAULT_RECONSOLIDATE_TIMEOUT_S: Final[float] = 30.0


def _dsn_from_config(config: 'MemexConfig') -> str:
    """Derive a plain `postgresql://` DSN for asyncpg from the meta_store config.

    Mirrors `scheduler.py:150-151` — strip the `+asyncpg` driver suffix that
    SQLAlchemy uses, since asyncpg wants the bare scheme.
    """
    sa_url = make_url(config.server.meta_store.instance.connection_string)
    return sa_url.set(drivername='postgresql').render_as_string(hide_password=False)


class LocksService:
    """Per-entity advisory-lock orchestration for reconsolidate / consolidate.

    Holds references to the metastore + contradiction engine + reflection
    service. Computes the asyncpg DSN once at init from `config`. `consolidate_vault`
    is added in TC-23-5.
    """

    def __init__(
        self,
        metastore: 'AsyncBaseMetaStoreEngine',
        config: 'MemexConfig',
        reflection: 'ReflectionService',
        contradiction: 'ContradictionEngine | None',
    ) -> None:
        self.metastore = metastore
        self.config = config
        self.reflection = reflection
        self.contradiction = contradiction
        self._dsn = _dsn_from_config(config)

    async def _resolve_entity_to_unit_ids(
        self,
        entity_id: UUID,
        vault_id: UUID,
    ) -> list[UUID]:
        """Return the list of MemoryUnit IDs linked to an entity in a vault.

        Vault-scoped per Wave 0 invariant — `Entity` is global but `UnitEntity`
        is vault-scoped, so cross-vault leakage is impossible by construction.
        """
        from memex_core.memory.sql_models import UnitEntity

        async with self.metastore.session() as session:
            stmt = (
                select(UnitEntity.unit_id)
                .where(UnitEntity.entity_id == entity_id)
                .where(UnitEntity.vault_id == vault_id)
            )
            result = await session.exec(stmt)
            return list(result)

    async def reconsolidate_entity(
        self,
        entity_id: UUID,
        vault_id: UUID,
        *,
        timeout_seconds: float = _DEFAULT_RECONSOLIDATE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Re-evaluate memories linked to an entity under a per-entity lock.

        Steps (RFC-005 / RFC-008):
            1. Acquire `acquire_entity_lock(entity_id)` for `timeout_seconds`.
            2. Resolve `entity_id → unit_ids` via UnitEntity, scoped to vault.
            3. ContradictionEngine.detect_contradictions on those unit_ids.
            4. ReflectionService.reflect_batch with [ReflectionRequest(entity_id)].
            5. Return summary.

        Raises EntityLockTimeoutError if another reconsolidate is in flight.
        """
        from memex_core.memory.reflect.models import ReflectionRequest

        log = logger.getChild(str(entity_id))
        async with acquire_entity_lock(self._dsn, entity_id, timeout_seconds=timeout_seconds):
            unit_ids = await self._resolve_entity_to_unit_ids(entity_id, vault_id)
            log.info(
                'reconsolidate.start',
                extra={
                    'entity_id': str(entity_id),
                    'vault_id': str(vault_id),
                    'unit_count': len(unit_ids),
                },
            )

            contradictions_run = 0
            if unit_ids and self.contradiction is not None:
                await self.contradiction.detect_contradictions(
                    session_factory=self.metastore.session_maker(),
                    document_id=None,
                    unit_ids=unit_ids,
                    vault_id=vault_id,
                )
                contradictions_run = len(unit_ids)

            results = await self.reflection.reflect_batch(
                [
                    ReflectionRequest(
                        entity_id=entity_id,
                        vault_id=vault_id,
                        limit_recent_memories=None,
                    )
                ]
            )
            mental_model_id: str | None = None
            observations_added = 0
            if results:
                result = results[0]
                mental_model_id = (
                    str(result.updated_model.id) if result.updated_model.id is not None else None
                )
                observations_added = len(result.new_observations)

            log.info(
                'reconsolidate.complete',
                extra={
                    'entity_id': str(entity_id),
                    'units_examined': len(unit_ids),
                    'contradictions_run': contradictions_run,
                    'observations_added': observations_added,
                },
            )
            return {
                'entity_id': str(entity_id),
                'vault_id': str(vault_id),
                'units_examined': len(unit_ids),
                'contradictions_run': contradictions_run,
                'mental_model_id': mental_model_id,
                'observations_added': observations_added,
            }
