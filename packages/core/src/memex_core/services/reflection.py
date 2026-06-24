"""Reflection service — reflection and mental model synthesis."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal
from uuid import UUID

import dspy
from sqlalchemy import text

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.config import MemexConfig
from memex_core.context import background_session
from memex_core.memory.engine import MemoryEngine
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.reflect.models import (
    ReflectionRequest,
    ReflectionResult,
)
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.memory.sql_models import Observation, ReflectionQueue
from memex_core.memory.models.protocols import EmbeddingsModel
from memex_core.services.audit import AuditService, audit_event
from memex_core.services.rate_limit import (
    TokenBucketRateLimiter,
)
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.reflection')


from memex_core.memory.reflect.exceptions import ReflectionAbandonedError

SummarizeScope = Literal['incremental', 'full']


class ReflectionService:
    """Reflection operations.

    Unlike other services, ReflectionService has heavier dependencies
    because reflection interacts with the memory engine, LLM, and queue.
    """

    _audit_service: AuditService | None = None

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        config: MemexConfig,
        lm: dspy.LM,
        memory: MemoryEngine,
        extraction: ExtractionEngine,
        queue_service: ReflectionQueueService,
        embedding_model: EmbeddingsModel,
    ) -> None:
        self.metastore = metastore
        self.config = config
        self.lm = lm
        self.memory = memory
        self._extraction = extraction
        self.queue_service = queue_service
        self.embedding_model = embedding_model
        self._reflection_lock = asyncio.Lock()
        rate_limit_cfg = config.server.memory.reflection.summarize_node_rate_limit
        self._summarize_node_limiter = TokenBucketRateLimiter(
            per_seconds=rate_limit_cfg.per_entity_per_seconds,
            burst=rate_limit_cfg.burst,
            max_keys=rate_limit_cfg.max_keys,
            enabled=rate_limit_cfg.enabled,
        )

    async def background_reflect(self, request: ReflectionRequest) -> None:
        """Run reflection in the background, ensuring serialization via lock."""
        async with background_session('bg-reflect'):
            async with self._reflection_lock:
                try:
                    logger.info(f'Starting background reflection for entity {request.entity_id}')
                    await self.reflect(request)
                    logger.info(f'Completed background reflection for entity {request.entity_id}')
                except ReflectionAbandonedError as exc:
                    # Benign concurrency contention — the entity was already
                    # re-enqueued by reflect() via mark_abandoned. Log at INFO
                    # so production alerting (which typically pages on
                    # error-level reflection failures) is not woken up.
                    # Abandon volume is observable via
                    # ``memex_reflection_cas_abandons_total``.
                    logger.info(
                        'Background reflection abandoned for entity %s '
                        '(re-enqueued for next tick): %s',
                        request.entity_id,
                        exc,
                    )
                except Exception as e:
                    logger.error(
                        f'Error during background reflection for entity {request.entity_id}: {e}',
                        exc_info=True,
                    )

    async def background_reflect_batch(self, requests: list[ReflectionRequest]) -> None:
        """Run batch reflection in the background, ensuring serialization via lock."""
        if not requests:
            return

        async with background_session('bg-reflect'):
            async with self._reflection_lock:
                try:
                    entity_ids = [str(r.entity_id) for r in requests]
                    logger.info(f'Starting background batch reflection for entities: {entity_ids}')
                    await self.reflect_batch(requests)
                    logger.info(
                        f'Completed background batch reflection for {len(requests)} entities'
                    )
                except Exception as e:
                    logger.error(f'Error during background batch reflection: {e}', exc_info=True)

    async def reflect(self, request: ReflectionRequest) -> ReflectionResult:
        """Reflect on a single entity to update its Mental Model."""
        async with self.metastore.session() as session:
            from memex_core.memory.reflect.reflection import ReflectionEngine

            reflector = ReflectionEngine(
                session,
                self.config,
                self.embedding_model,
                entity_session_factory=self.metastore.session,
            )

            models, abandoned_list, failed_list = await reflector.reflect_batch([request])
            abandoned_ids = set(abandoned_list)
            failed_ids = set(failed_list)
            if not models:
                if request.entity_id in abandoned_ids:
                    # CAS abandon — re-enqueue without retry_count increment,
                    # then raise so the caller (summarize_node / HTTP / MCP)
                    # can translate to a "try again" envelope. Returning a
                    # synthetic empty MentalModel here would silently misrepresent
                    # the entity as observation-less to the agent / user.
                    await self.queue_service.mark_abandoned(
                        session,
                        entity_id=request.entity_id,
                        vault_id=request.vault_id,
                    )
                    raise ReflectionAbandonedError(
                        f'Reflection for entity {request.entity_id} abandoned '
                        f'because a concurrent worker advanced the version. '
                        f'Re-enqueued for the next scheduler tick.'
                    )
                if request.entity_id in failed_ids:
                    # Real failure (exception in the engine path).
                    await self.queue_service.mark_failed(
                        session,
                        entity_id=request.entity_id,
                        vault_id=request.vault_id,
                        error=f'Reflection produced no models for entity {request.entity_id}',
                    )
                else:
                    # Skipped: the entity's only mental model is archived — there
                    # is nothing to reflect. Resolve the queue task rather than
                    # failing it toward DEAD_LETTER (mirrors reflect_batch_detailed).
                    logger.info(
                        f'Resolving reflection task for archived-only entity {request.entity_id}'
                    )
                    await self.queue_service.complete_reflection(
                        session, [request.entity_id], vault_id=request.vault_id
                    )
                from memex_core.memory.sql_models import MentalModel

                return ReflectionResult(
                    entity_id=request.entity_id,
                    new_observations=[],
                    updated_model=MentalModel(
                        entity_id=request.entity_id, vault_id=request.vault_id
                    ),
                )

            mental_model = models[0]

            await self.queue_service.complete_reflection(
                session, [request.entity_id], vault_id=request.vault_id
            )

            audit_event(
                self._audit_service,
                'reflection.triggered',
                'entity',
                str(request.entity_id),
                vault_id=str(request.vault_id),
            )
            return ReflectionResult(
                entity_id=request.entity_id,
                new_observations=[Observation(**o) for o in mental_model.observations],
                updated_model=mental_model,
            )

    async def summarize_node(
        self,
        entity_id: UUID,
        *,
        scope: SummarizeScope = 'incremental',
        vault_id: UUID | None = None,
    ) -> ReflectionResult:
        """Synchronous on-demand reflection for a single entity.

        Wraps :meth:`reflect` with a per-(entity_id, vault_id) token-bucket
        rate limit. ``scope='incremental'`` (default) honours the standard
        20-unit window; ``scope='full'`` re-evaluates all evidence on the
        entity (capped at MAX_FULL_SCOPE_UNITS by the engine).

        Raises ``RateLimitExceededError`` (with ``retry_after_seconds``) if
        the bucket is empty for this (entity, vault) key. The exception is a
        service-layer signal — surface translation to MCP/HTTP belongs to
        the calling layer.
        """
        effective_vault = vault_id or GLOBAL_VAULT_ID
        await self._summarize_node_limiter.acquire((entity_id, effective_vault))

        limit_recent = None if scope == 'full' else 20
        request = ReflectionRequest(
            entity_id=entity_id,
            vault_id=effective_vault,
            limit_recent_memories=limit_recent,
        )
        result = await self.reflect(request)
        audit_event(
            self._audit_service,
            'memory_summarize_node',
            'entity',
            str(entity_id),
            scope=scope,
            vault_id=str(effective_vault),
            observation_count=len(result.new_observations),
        )
        return result

    async def reflect_batch_detailed(
        self, requests: list[ReflectionRequest]
    ) -> tuple[list[ReflectionResult], list[UUID]]:
        """Reflect on multiple entities in parallel and return both
        applied results AND CAS-abandoned entity_ids.

        Callers that need to distinguish "successfully reflected with no
        new observations" from "concurrent worker won the CAS race"
        should use this variant. ``reflect_batch`` discards the abandon
        list for backward compatibility with callers that don't care.

        Concurrency: each call constructs its own ReflectionEngine and
        the abandon list rides back on the engine's return tuple — no
        shared service-instance state is touched, so concurrent
        invocations cannot race.
        """
        if not requests:
            return [], []

        async with self.metastore.session() as session:
            from memex_core.memory.reflect.reflection import ReflectionEngine

            reflector = ReflectionEngine(
                session,
                self.config,
                self.embedding_model,
                entity_session_factory=self.metastore.session,
            )

            # The engine tracks three disjoint outcomes per entity:
            # ``models`` (Phase 5 CAS UPDATE applied), ``abandoned_list``
            # (CAS lost the version race — benign contention), and
            # ``failed_list`` (a real exception inside the per-entity
            # pipeline). Routing CAS abandons through mark_failed would
            # increment retry_count and eventually DEAD_LETTER an
            # entity that is merely contended; routing real failures
            # through mark_abandoned would carousel a broken entity
            # forever without ever surfacing the failure to operators.
            models, abandoned_list, failed_list = await reflector.reflect_batch(requests)
            abandoned_ids = set(abandoned_list)
            failed_ids = set(failed_list)

            from collections import defaultdict

            processed_by_vault = defaultdict(list)
            for m in models:
                processed_by_vault[m.vault_id].append(m.entity_id)

            # Order is intentional: complete_reflection's commit fires first
            # (deletes succeeded queue rows). The two subsequent loops then
            # run in a fresh SQLAlchemy autobegin transaction on the same
            # session — a failure in either does not roll back the deletes.
            for vid, eids in processed_by_vault.items():
                await self.queue_service.complete_reflection(session, eids, vault_id=vid)

            # Build a (entity_id, vault_id) lookup so we can route on
            # the request's own vault_id — falling back to "find any
            # request" would risk routing a failure to the wrong vault
            # if duplicates ever slipped past the engine's dedup.
            req_vaults: dict[UUID, UUID] = {}
            for req in requests:
                req_vaults.setdefault(req.entity_id, req.vault_id)

            # CAS-abandoned: re-enqueue (PENDING) without incrementing
            # retry_count. SKIP LOCKED on the next tick re-claims them.
            for eid in abandoned_ids:
                await self.queue_service.mark_abandoned(
                    session,
                    entity_id=eid,
                    vault_id=req_vaults.get(eid, GLOBAL_VAULT_ID),
                )

            # Real failures (exceptions in _process_entity_reflection):
            # mark_failed, increments retry_count toward DEAD_LETTER.
            for eid in failed_ids:
                await self.queue_service.mark_failed(
                    session,
                    entity_id=eid,
                    vault_id=req_vaults.get(eid, GLOBAL_VAULT_ID),
                    error=f'Reflection failed for entity {eid}',
                )

            succeeded_ids = {m.entity_id for m in models}

            # Entities the engine SKIPPED (no ACTIVE mental model — only an
            # archived one exists) are absent from all three outcome lists by
            # design (see _process_entity_reflection). Resolve their queue task:
            # there is nothing to reflect, so completing it stops the row from
            # sitting in PROCESSING until stale-recovery re-enqueues it forever,
            # and avoids failing it toward DEAD_LETTER.
            skipped_by_vault: dict[UUID, list[UUID]] = defaultdict(list)
            for req in requests:
                if (
                    req.entity_id not in succeeded_ids
                    and req.entity_id not in abandoned_ids
                    and req.entity_id not in failed_ids
                ):
                    skipped_by_vault[req.vault_id].append(req.entity_id)
            for vid, eids in skipped_by_vault.items():
                logger.info(
                    f'Resolving {len(eids)} reflection task(s) for archived-only '
                    f'entit{"y" if len(eids) == 1 else "ies"} in vault {vid}'
                )
                await self.queue_service.complete_reflection(session, eids, vault_id=vid)

            results = []
            for model in models:
                results.append(
                    ReflectionResult(
                        entity_id=model.entity_id,
                        new_observations=list(model.observations),
                        updated_model=model,
                    )
                )

            for req in requests:
                if req.entity_id in succeeded_ids:
                    audit_event(
                        self._audit_service,
                        'reflection.triggered',
                        'entity',
                        str(req.entity_id),
                        vault_id=str(req.vault_id),
                    )
            return results, abandoned_list

    async def reflect_batch(self, requests: list[ReflectionRequest]) -> list[ReflectionResult]:
        """Reflect on multiple entities in parallel using a single DB session.

        Backward-compatible thin wrapper around ``reflect_batch_detailed``
        that drops the CAS-abandoned entity_ids. Callers that need to
        distinguish abandons from "no new observations" should call
        ``reflect_batch_detailed`` directly.
        """
        results, _abandoned = await self.reflect_batch_detailed(requests)
        return results

    async def get_reflection_queue_batch(
        self,
        limit: int = 10,
        vault_id: UUID | None = None,
        vault_ids: list[UUID] | None = None,
    ) -> list[Any]:
        """Get the next batch of items from the reflection queue."""
        ids = list(vault_ids) if vault_ids else []
        if vault_id and vault_id not in ids:
            ids.append(vault_id)
        async with self.metastore.session() as session:
            return await self.queue_service.get_next_batch(
                session,
                limit=limit,
                vault_ids=ids or None,
            )

    async def claim_reflection_queue_batch(
        self, limit: int = 10, vault_id: UUID | None = None
    ) -> list[Any]:
        """Claim and lock the next batch of items from the reflection queue."""
        async with self.metastore.session() as session:
            return await self.queue_service.claim_next_batch(
                session, limit=limit, vault_id=vault_id
            )

    async def refresh_observation(self, item: 'ReflectionQueue') -> None:
        """Execute a single refresh-observation task end-to-end.

        Constructs a per-call ``ReflectionEngine`` (mirrors the existing
        per-batch construction in ``reflect_batch_detailed``) and invokes
        the engine's ``_refresh_observation`` — which opens its own short
        per-phase sessions internally (Phase A read, Phase C write). On
        success, opens a fresh session to delete the queue row via
        ``complete_refresh``.

        No outer session is held across the LLM call: holding a pool
        connection idle while the LLM round-trip runs would halve effective
        pool size under refresh load.

        Raises ``AdvisoryLockTakenError`` for the scheduler to treat as a
        reclaim-without-retry-bump. Other exceptions propagate to the
        scheduler's per-item error handler which calls ``mark_failed``.
        """
        from memex_core.memory.reflect.reflection import get_reflection_engine

        # The refresh path uses ``entity_session_factory`` exclusively —
        # the engine never reads ``self.session``. We still pass session=
        # None because ``_entity_session`` raises a loud RuntimeError if
        # any future code path on this engine instance accidentally falls
        # through to ``self.session``.
        engine = get_reflection_engine(
            session=None,  # type: ignore[arg-type]
            config=self.config,
            embedder=self.embedding_model,
            entity_session_factory=self.metastore.session,
        )
        await engine._refresh_observation(item)
        async with self.metastore.session() as session:
            await self.queue_service.complete_refresh(session, item)

    async def reclaim_refresh_with_backoff(self, item: 'ReflectionQueue') -> None:
        """Reset a refresh task to PENDING with a jittered last_queued_at.

        Used by the scheduler when ``_refresh_observation`` raises
        ``AdvisoryLockTakenError``. ``retry_count`` is NOT incremented —
        advisory-lock contention is transient, not a failure.
        """
        import random

        cfg = self.config.server.memory.reflection
        jitter = random.uniform(
            float(cfg.refresh_obs_retry_backoff_min_seconds),
            float(cfg.refresh_obs_retry_backoff_max_seconds),
        )
        async with self.metastore.session() as session:
            await self.queue_service.reclaim_with_backoff(session, item, jitter)

    async def mark_item_failed(self, item: 'ReflectionQueue', error: str) -> None:
        """Mark a specific claimed queue item as failed, filtered by task_type.

        Refresh-observation failures filter by ``observation_id`` so they
        don't bump retry_count on a co-pending reflect task for the same
        entity.
        """
        async with self.metastore.session() as session:
            await self.queue_service.mark_failed(
                session,
                entity_id=item.entity_id,
                vault_id=item.vault_id,
                error=error,
                task_type=getattr(item, 'task_type', 'reflect') or 'reflect',
                observation_id=getattr(item, 'observation_id', None),
            )

    async def reconcile_missing_refresh_tasks(self, vault_id: UUID, batch_size: int = 50) -> int:
        """Repair deprio'd MUs that lack a refresh-observation queue row.

        Scoped to a single ``vault_id`` to avoid cross-tenant noise. Returns
        the number of refresh rows enqueued by the repair pass.
        """
        from memex_core.metrics import (
            REFRESH_OBSERVATION_RECONCILE_REPAIRED_TOTAL,
        )

        async with self.metastore.session() as session:
            # The NOT EXISTS subquery filters to in-flight statuses only —
            # without ``status IN ('pending', 'processing')`` a stuck
            # DEAD_LETTER (or FAILED, ABANDONED) row would suppress
            # re-enqueue indefinitely, leaving the MU's observations stale
            # permanently. The dead-letter recovery path is separate (operator
            # action / mark_failed → mark_abandoned cycle).
            from sqlalchemy import Integer as SAInteger
            from sqlalchemy import bindparam

            # ``LIMIT :limit`` with a bare int parameter can trip asyncpg's
            # type inference ("cannot determine data type for parameter $N").
            # Pin the type explicitly — same discipline as the ``ARRAY(String)``
            # bind on ``flush_deferred_observation_refresh``'s ``:probes``.
            stmt = text(
                'SELECT mu.id FROM memory_units mu '
                'WHERE mu.vault_id = :vault_id '
                'AND mu.is_deprioritized = TRUE '
                'AND NOT EXISTS ('
                '    SELECT 1 FROM reflection_queue rq '
                '    WHERE rq.vault_id = mu.vault_id '
                '    AND rq.source_unit_id = mu.id '
                "    AND rq.task_type = 'refresh_observation'"
                "    AND rq.status IN ('pending', 'processing')"
                ') '
                'LIMIT :limit'
            ).bindparams(bindparam('limit', type_=SAInteger))
            scan = await session.execute(
                stmt,
                {'vault_id': vault_id, 'limit': batch_size},
            )
            unit_ids = [row[0] for row in scan.all()]
            if not unit_ids:
                return 0

        # Construct a thin UnitsService on demand — flush_deferred_observation_refresh
        # only needs metastore + config; filestore is required by BaseService but the
        # flush helper never touches it. Reusing the ReflectionService's metastore
        # keeps every refresh enqueue in the same connection pool.
        from memex_core.services.units import UnitsService

        units_service = UnitsService(
            metastore=self.metastore,
            filestore=None,  # type: ignore[arg-type]  # flush path doesn't read filestore
            config=self.config,
        )
        enqueued = await units_service.flush_deferred_observation_refresh(
            unit_ids, vault_id=vault_id
        )
        if enqueued:
            REFRESH_OBSERVATION_RECONCILE_REPAIRED_TOTAL.labels(vault_id=str(vault_id)).inc(
                enqueued
            )
        return enqueued

    async def recover_stale_processing(self) -> int:
        """Reset PROCESSING items stuck longer than the configured timeout."""
        async with self.metastore.session() as session:
            return await self.queue_service.recover_stale_processing(session)

    async def queue_observability_snapshot(self) -> tuple[dict[str, int], float]:
        """Cheap aggregates for the scheduler's Prometheus gauge refresh."""
        async with self.metastore.session() as session:
            return await self.queue_service.observability_snapshot(session)

    async def get_dead_letter_items(
        self,
        limit: int = 50,
        offset: int = 0,
        vault_id: UUID | None = None,
    ) -> list[Any]:
        """List dead-lettered reflection tasks."""
        async with self.metastore.session() as session:
            return await self.queue_service.get_dead_letter_items(
                session, limit=limit, offset=offset, vault_id=vault_id
            )

    async def retry_dead_letter_item(self, item_id: UUID) -> Any:
        """Retry a dead-lettered reflection task by resetting it to pending."""
        async with self.metastore.session() as session:
            result = await self.queue_service.retry_dead_letter(session, item_id)
            audit_event(self._audit_service, 'reflection.dlq_retried', 'reflection', str(item_id))
            return result
