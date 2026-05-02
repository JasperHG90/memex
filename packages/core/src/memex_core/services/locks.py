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
from memex_core.services.rate_limit import TokenBucketRateLimiter

if TYPE_CHECKING:
    from memex_core.config import MemexConfig
    from memex_core.memory.contradiction.engine import ContradictionEngine
    from memex_core.services.reflection import ReflectionService
    from memex_core.services.units import UnitsService
    from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.locks')

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
    the leader advisory lock (~2^52, defined in ``memex_core.scheduler``).
    Deterministic by construction — the same UUID yields the same lock_id
    across processes (no PYTHONHASHSEED dependency).
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

# CRIT-1 mitigation: bound concurrent asyncpg connections opened by
# acquire_entity_lock so a burst of reconsolidate/consolidate calls cannot
# exhaust Postgres ``max_connections``. Sized to match the SQLAlchemy pool
# default (``pool_size=5`` in metastore.py). Module-level so it covers both
# the LocksService and ConsolidationService callers. The full fix (a shared
# asyncpg pool reused across acquisitions) is tracked as a follow-up.
_MAX_CONCURRENT_LOCK_CONNECTIONS: Final[int] = 5
_lock_connection_semaphore: asyncio.Semaphore | None = None


def _get_lock_connection_semaphore() -> asyncio.Semaphore:
    """Lazily create the connection semaphore on first use.

    Created lazily so the running event loop is the one that owns the
    semaphore; constructing it at module import would bind to whatever
    loop happens to be active (or none) at import time.
    """
    global _lock_connection_semaphore
    if _lock_connection_semaphore is None:
        _lock_connection_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_LOCK_CONNECTIONS)
    return _lock_connection_semaphore


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

    Connection acquisition is gated by a process-level semaphore so a burst
    of concurrent callers cannot exhaust Postgres ``max_connections``.

    Raises EntityLockTimeoutError when timeout_seconds elapses without
    acquiring. On context exit (success OR exception), the lock is released
    and the connection closed; if the process crashes before exit, Postgres
    auto-releases on backend termination (POC TC-12-5).
    """
    from memex_core.metrics import ENTITY_LOCK_ACQUIRES_TOTAL

    lock_id = entity_lock_id(entity_id)
    semaphore = _get_lock_connection_semaphore()
    async with semaphore:
        conn = await asyncpg.connect(dsn)
        acquired = False
        try:
            deadline = monotonic() + timeout_seconds
            while True:
                got = await conn.fetchval('SELECT pg_try_advisory_lock($1)', lock_id)
                if got:
                    acquired = True
                    ENTITY_LOCK_ACQUIRES_TOTAL.labels(outcome='acquired').inc()
                    break
                if monotonic() >= deadline:
                    ENTITY_LOCK_ACQUIRES_TOTAL.labels(outcome='timeout').inc()
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
    service + units service. Computes the asyncpg DSN once at init from
    `config`. `consolidate_vault` does NOT acquire an advisory lock (per
    RFC-008 §`memex_memory_consolidate` — per-row F4 deprioritize is
    idempotent, so no batch lock is needed).
    """

    def __init__(
        self,
        metastore: 'AsyncBaseMetaStoreEngine',
        config: 'MemexConfig',
        reflection: 'ReflectionService',
        contradiction: 'ContradictionEngine | None',
        units: 'UnitsService | None' = None,
    ) -> None:
        self.metastore = metastore
        self.config = config
        self.reflection = reflection
        self.contradiction = contradiction
        self.units = units
        self._dsn = _dsn_from_config(config)
        # F9 / RFC-008 line 125: per-vault rate limit on memex_memory_consolidate.
        # Reuses F5's TokenBucketRateLimiter primitive. Default 1 call per vault
        # per hour (LLM-intensive + mass-mutation guard).
        consolidate_cfg = config.server.memory.consolidate_rate_limit
        self._consolidate_limiter = TokenBucketRateLimiter(
            per_seconds=consolidate_cfg.per_vault_per_seconds,
            burst=consolidate_cfg.burst,
            max_keys=consolidate_cfg.max_keys,
            enabled=consolidate_cfg.enabled,
        )

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
        from memex_core.metrics import RECONSOLIDATE_TOTAL

        log = logger.getChild(str(entity_id))
        try:
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
                        str(result.updated_model.id)
                        if result.updated_model.id is not None
                        else None
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
                RECONSOLIDATE_TOTAL.labels(outcome='success').inc()
                return {
                    'entity_id': str(entity_id),
                    'vault_id': str(vault_id),
                    'units_examined': len(unit_ids),
                    'contradictions_run': contradictions_run,
                    'mental_model_id': mental_model_id,
                    'observations_added': observations_added,
                }
        except EntityLockTimeoutError:
            RECONSOLIDATE_TOTAL.labels(outcome='lock_timeout').inc()
            raise
        except Exception:
            RECONSOLIDATE_TOTAL.labels(outcome='error').inc()
            raise

    async def _select_consolidate_candidates(self, vault_id: UUID) -> list[UUID]:
        """Identify low-MW + 5+-outcomes + non-deprioritized + 30-days-old units.

        Predicate (RFC-008 §`memex_memory_consolidate`):
            ``mw_score < 0.35
              AND (success_co_count + failure_co_count) >= 5
              AND is_deprioritized = false
              AND created_at < now() - INTERVAL '30 days'``

        Threshold 0.35 is intentionally broader than F6 `cold_low_mw_unit`
        (0.3) — that rule proposes; this verb proposes-and-acts.
        """
        from datetime import datetime, timedelta, timezone

        from memex_core.memory.sql_models import MemoryUnit

        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        async with self.metastore.session() as session:
            stmt = (
                select(MemoryUnit.id)
                .where(MemoryUnit.vault_id == vault_id)
                .where(MemoryUnit.status == 'active')
                .where(MemoryUnit.is_deprioritized.is_(False))
                .where((MemoryUnit.success_co_count + MemoryUnit.failure_co_count) >= 5)
                .where(
                    (
                        (MemoryUnit.success_co_count + 1.0)
                        / (MemoryUnit.success_co_count + MemoryUnit.failure_co_count + 2)
                    )
                    < 0.35
                )
                .where(MemoryUnit.created_at < cutoff)
                .order_by(MemoryUnit.created_at.asc())
            )
            result = await session.exec(stmt)
            return list(result)

    async def consolidate_vault(
        self,
        vault_id: UUID,
        *,
        dry_run: bool = False,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Vault-wide low-MW unit consolidation (RFC-008).

        Steps:
            1. Acquire a per-vault rate-limit token (RFC-008 line 125;
               default 1 call per vault per hour). Skipped on `dry_run`
               so agents can preview cheaply without burning the bucket.
            2. Identify candidates by predicate (see `_select_consolidate_candidates`).
            3. If `dry_run`: return preview, no writes.
            4. Otherwise: for each candidate, call F4
               `UnitsService.set_unit_deprioritized` and write a
               `MaintenanceProposal` row with `status='resolved'`,
               `rule_name='consolidate_vault_low_mw'`, evidence carrying
               actor + reason for traceability.

        Does NOT acquire an advisory lock — per-row F4 deprioritize is
        idempotent (column flip).

        Branch B fallback: if `maintenance_proposals` table is missing
        (F6 not deployed), returns the preview shape without proposal
        writes. Branch A is live in this codebase since F6 merged.

        Raises ``RateLimitExceededError`` (with ``retry_after_seconds``)
        when the per-vault bucket is empty and ``dry_run=False``. The
        exception is a service-layer signal — surface translation to
        MCP/HTTP belongs to the calling layer (mirrors F5 summarize_node).
        """
        from sqlalchemy import inspect as sa_inspect

        from memex_core.metrics import CONSOLIDATE_TOTAL

        if not dry_run:
            self._consolidate_limiter.acquire(vault_id)

        try:
            candidates = await self._select_consolidate_candidates(vault_id)
            log = logger.getChild(str(vault_id))
            log.info(
                'consolidate.start',
                extra={
                    'vault_id': str(vault_id),
                    'candidates': len(candidates),
                    'dry_run': dry_run,
                },
            )

            if dry_run:
                CONSOLIDATE_TOTAL.labels(outcome='success').inc()
                return {
                    'vault_id': str(vault_id),
                    'dry_run': True,
                    'candidates': len(candidates),
                    'units_deprioritized': 0,
                    'proposals_written': 0,
                }

            has_proposals_table = await self._has_maintenance_proposals_table(sa_inspect)
            units_deprioritized = 0
            proposals_written = 0
            reason = 'vault-wide consolidate: low MW + 5+ outcomes after 30d'

            if self.units is None:
                raise RuntimeError(
                    'consolidate_vault requires UnitsService — wire it via '
                    'LocksService(..., units=...) at construction'
                )

            for unit_id in candidates:
                await self.units.set_unit_deprioritized(unit_id, reason, actor=actor)
                units_deprioritized += 1
                if has_proposals_table:
                    await self._write_consolidate_proposal(
                        unit_id=unit_id,
                        vault_id=vault_id,
                        actor=actor,
                        reason=reason,
                    )
                    proposals_written += 1

            log.info(
                'consolidate.complete',
                extra={
                    'vault_id': str(vault_id),
                    'units_deprioritized': units_deprioritized,
                    'proposals_written': proposals_written,
                },
            )
            CONSOLIDATE_TOTAL.labels(outcome='success').inc()
            return {
                'vault_id': str(vault_id),
                'dry_run': False,
                'candidates': len(candidates),
                'units_deprioritized': units_deprioritized,
                'proposals_written': proposals_written,
            }
        except Exception:
            CONSOLIDATE_TOTAL.labels(outcome='error').inc()
            raise

    async def _has_maintenance_proposals_table(self, sa_inspect: Any) -> bool:
        engine = self.metastore.engine
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: sa_inspect(sync_conn).has_table('maintenance_proposals')
            )

    async def _write_consolidate_proposal(
        self,
        *,
        unit_id: UUID,
        vault_id: UUID,
        actor: str | None,
        reason: str,
    ) -> None:
        from datetime import datetime, timezone

        from memex_core.memory.sql_models import (
            LintSource,
            LintStatus,
            LintType,
            MaintenanceProposal,
        )

        async with self.metastore.session() as session:
            proposal = MaintenanceProposal(
                vault_id=vault_id,
                lint_type=LintType.QUALITY,
                target_type='memory_unit',
                target_id=str(unit_id),
                rule_name='consolidate_vault_low_mw',
                evidence={
                    'reason': reason,
                    'resolved_by': 'memex_memory_consolidate',
                    'actor': actor,
                },
                suggested_action=(
                    'Unit deprioritized by vault-wide consolidate (low MW + 5+ outcomes after 30d).'
                ),
                status=LintStatus.RESOLVED,
                source=LintSource.RULE,
                resolved_at=datetime.now(timezone.utc),
                resolved_by=actor or 'memex_memory_consolidate',
            )
            session.add(proposal)
            await session.commit()
