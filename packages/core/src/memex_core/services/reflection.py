"""Reflection service — reflection and mental model synthesis."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal
from uuid import UUID

import dspy

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
from memex_core.memory.sql_models import Observation
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

            models, abandoned_list, _failed_list = await reflector.reflect_batch([request])
            abandoned_ids = set(abandoned_list)
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
                # Real failure (exception in the engine path).
                await self.queue_service.mark_failed(
                    session,
                    entity_id=request.entity_id,
                    vault_id=request.vault_id,
                    error=f'Reflection produced no models for entity {request.entity_id}',
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

    async def recover_stale_processing(self) -> int:
        """Reset PROCESSING items stuck longer than the configured timeout."""
        async with self.metastore.session() as session:
            return await self.queue_service.recover_stale_processing(session)

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
