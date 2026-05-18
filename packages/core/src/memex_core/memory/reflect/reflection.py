"""Hindsight Reflect Engine — orchestrates Phases 0-6 to update Mental Models."""

import asyncio
import logging
from collections import defaultdict
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

import dspy
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import func, update as sa_update
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import defer
from sqlalchemy.orm.attributes import flag_modified, set_committed_value

EntitySessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

from memex_core.config import MemexConfig, GLOBAL_VAULT_ID
from memex_core.llm import run_dspy_operation
from memex_core.tracing import trace_span
from memex_core.memory.sql_models import (
    ContentStatus,
    Entity,
    MemoryUnit,
    MentalModel,
    Observation,
    EvidenceItem,
    ReflectionQueue,
    UnitEntity,
)
from memex_core.memory.reflect.entity_locks import get_entity_lock
from memex_core.memory.reflect.exceptions import (
    ReflectionAbandonedError,
    RefreshCASAbandonedError,
    RefreshStaleReadError,
)
from memex_core.memory.reflect.models import ReflectionRequest
from memex_core.memory.reflect.prompts import (
    SeedPhaseSignature,
    ValidatePhaseSignature,
    ValidatedObservation,
    ComparePhaseSignature,
    CandidateObservation,
    UnvalidatedCandidateObservation,
    ReflectMemoryContext,
    ReflectObservationContext,
    UpdateExistingSignature,
    ReflectEvidenceContext,
    ReflectComparisonObservation,
    EnrichmentSignature,
    RefreshedObservation,
    RefreshObservationSignature,
)
from memex_core.memory.reflect.utils import (
    build_memory_context,
    create_citation_map,
    parse_timestamp,
)
from memex_core.memory.reflect.trends import compute_trend
from memex_core.memory.models.protocols import EmbeddingsModel
from memex_core.memory.formatting import format_for_embedding
from memex_core.memory.confidence import extract_confidence_and_count, mean_and_variance
from memex_core.metrics import (
    PHASE4_PROVENANCE_MALFORMED_TOTAL,
    REFLECTION_CAS_ABANDONS_TOTAL,
    REFRESH_OBSERVATION_DROP_OVERRIDDEN_TOTAL,
    REFRESH_OBSERVATION_EMPTY_CONTENT_COERCED_TOTAL,
    REFRESH_OBSERVATION_MERGED_PREDECESSOR_TOTAL,
    REFRESH_OBSERVATION_TASK_ALREADY_ABSORBED_TOTAL,
    REFRESH_OBSERVATION_TASK_COMPLETED_TOTAL,
    REFRESH_OBSERVATION_TASK_DROPPED_BY_LLM_TOTAL,
    REFRESH_OBSERVATION_TASK_LATENCY_SECONDS,
    REFRESH_OBSERVATION_TASK_OBS_ALREADY_PRUNED_TOTAL,
    REFRESH_OBSERVATION_TASK_ZERO_EVIDENCE_TOTAL,
)

logger = logging.getLogger('memex.core.memory.reflect.reflection')


def _variance_key(unit: MemoryUnit) -> float:
    """Beta(1,1) variance for reflection prioritisation. Pure — no captured state."""
    confidence, count = extract_confidence_and_count(unit)
    _, variance = mean_and_variance(confidence, count)
    return variance


# Per-call cap when limit_recent_memories=None ('full' scope). Guards LLM cost;
# rate-limiting guards call frequency.
MAX_FULL_SCOPE_UNITS = 100


def get_reflection_engine(
    session: AsyncSession,
    config: MemexConfig,
    embedder: EmbeddingsModel,
    entity_session_factory: EntitySessionFactory | None = None,
) -> 'ReflectionEngine':
    """Factory for ReflectionEngine.

    For the refresh-observation path, callers MUST pass an
    ``entity_session_factory`` — that path opens its own per-phase sessions
    through the factory and does not read ``self.session``. The shared
    ``session`` is only used for the batch reflect path. ``_entity_session``
    raises a clear ``RuntimeError`` if both ``self.session`` is None AND
    ``entity_session_factory`` is None, so the misconfiguration surfaces at
    first access rather than as a silent ``AttributeError``.
    """
    return ReflectionEngine(
        session=session,
        config=config,
        embedder=embedder,
        entity_session_factory=entity_session_factory,
    )


def _resolve_provenance_uuid(prov: Any | None, existing: list['Observation']) -> UUID:
    """Pick a stable UUID for a Phase 4 output observation from its provenance entry.

    Returns the lowest-index existing UUID for status='merged' or 'kept'; fresh uuid4()
    for status='added' or any malformed provenance (counter bumped with reason label).
    """
    if prov is None:
        return uuid4()
    status = getattr(prov, 'status', None)
    idxs = getattr(prov, 'merged_from_existing_indices', None) or []
    if status == 'added':
        return uuid4()
    # 'kept' and 'merged' both need at least one existing index; an empty list
    # is malformed — split the labels so prompt-drift can be diagnosed.
    if not idxs:
        if status == 'kept':
            PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='kept_no_indices').inc()
        elif status == 'merged':
            PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='merged_no_indices').inc()
        else:
            PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='unknown_status').inc()
        return uuid4()
    if any(i < 0 for i in idxs):
        PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='index_negative').inc()
        return uuid4()
    if any(i >= len(existing) for i in idxs):
        # 'existing_index_oob' distinguishes this from the 'output_index_oob'
        # bump in _phase_4_compare (where the bad index is into the OUTPUT
        # observations array, not the existing one).
        PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='existing_index_oob').inc()
        return uuid4()
    if len(set(idxs)) != len(idxs):
        PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='index_duplicate').inc()
        return uuid4()
    keep = existing[min(idxs)]
    # NOTE: MERGED_PREDECESSOR_TOTAL is bumped in the caller AFTER the
    # collision-detection pass, so a UUID that gets discarded due to
    # collision doesn't over-count as a real "predecessor dropped" event.
    return keep.id


def _drop_observation_in_place(mm: 'MentalModel', obs_id: UUID) -> None:
    """Remove the observation whose `id == obs_id` from `mm.observations` in place.

    Handles both dict-form (JSONB-loaded) and Observation-instance-form (mid-Phase-4)
    entries. The `_refresh_observation` path no longer relies on this helper —
    it computes the new observations list as a pure-Python operation outside
    the session and binds it via an explicit CAS UPDATE
    ``values(observations=new_observations)``. This helper is retained for
    test scaffolding (tests bypass the CAS path) and for any future caller
    that wants ORM-tracked in-place mutation; such a caller MUST persist via
    ``flag_modified(mm, 'observations')`` before commit, OR (preferred) use
    the CAS-UPDATE pattern from `_refresh_observation` Phase C.
    """
    obs_id_str = str(obs_id)
    mm.observations = [
        o
        for o in mm.observations
        if str(o.get('id') if isinstance(o, dict) else getattr(o, 'id', None)) != obs_id_str
    ]


class ReflectionEngine:
    """Orchestrates the periodic Reflection phase of the Hindsight architecture."""

    def __init__(
        self,
        session: AsyncSession,
        config: MemexConfig,
        embedder: EmbeddingsModel,
        entity_session_factory: EntitySessionFactory | None = None,
    ):
        # ``session`` is the orchestrator-level session used for the batch
        # fetches in ``reflect_batch`` (one-shot reads before LLM phases).
        #
        # ``entity_session_factory`` is the per-entity short-session
        # opener used inside each reflection's DB-touching phases — see
        # ``_entity_session``. Per-entity sessions release their
        # transactions between phases, so no DB transaction spans an LLM
        # call, eliminating the connection-pool exhaustion and VACUUM-
        # blocking behaviour of the pre-V18 shared-session model.
        # Production callers pass ``metastore.session`` (an async context
        # manager) here; legacy callers (unit tests) may omit it, in
        # which case the helper falls back to yielding ``self.session``
        # so existing single-session test scaffolds keep working.
        self.session = session
        self.entity_session_factory = entity_session_factory
        self.config = config or MemexConfig()
        self.embedder = embedder

        # Prefer injected LM; fall back to dspy.settings.lm, then extraction model.
        self.lm: dspy.LM | None
        try:
            if self.config.server.memory.reflection.model:
                model_config = self.config.server.memory.reflection.model
                self.lm = dspy.LM(
                    model=model_config.model,
                    api_base=str(model_config.base_url) if model_config.base_url else None,
                    api_key=model_config.api_key.get_secret_value()
                    if model_config.api_key
                    else None,
                    timeout=model_config.timeout,
                    num_retries=model_config.num_retries,
                )
            else:
                self.lm = dspy.settings.lm
                if not self.lm and self.config.server.memory.extraction.model:
                    model_config = self.config.server.memory.extraction.model
                    self.lm = dspy.LM(
                        model=model_config.model,
                        api_base=str(model_config.base_url) if model_config.base_url else None,
                        api_key=model_config.api_key.get_secret_value()
                        if model_config.api_key
                        else None,
                        timeout=model_config.timeout,
                        num_retries=model_config.num_retries,
                    )
        except (ValueError, RuntimeError, OSError, KeyError, AttributeError) as e:
            logger.warning(
                'Could not initialize LM: %s. Reflection might fail if LM is not set.', e
            )
            self.lm = None

    @asynccontextmanager
    async def _entity_session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session for *per-entity* DB work inside a reflection.

        When ``entity_session_factory`` is configured (production path),
        opens a fresh ``AsyncSession`` from the factory's async context
        manager. The transaction is released on context exit — so between
        LLM phases no DB transaction is held, no MVCC snapshot is pinned,
        and the connection returns to the pool.

        When ``entity_session_factory`` is None (legacy tests and the
        ``get_reflection_engine`` factory without an explicit override),
        falls back to yielding the shared orchestrator session. Single-
        session semantics — same behaviour as the pre-V18 code path.
        Production callers must plumb a factory in to unlock the per-
        entity transaction lifecycle.
        """
        if self.entity_session_factory is not None:
            async with self.entity_session_factory() as session:
                yield session
        else:
            if self.session is None:
                raise RuntimeError(
                    'ReflectionEngine has neither entity_session_factory nor '
                    'a shared session. Construct via get_reflection_engine() '
                    'with entity_session_factory=metastore.session for the '
                    'refresh-observation path, or pass a session for legacy '
                    'single-session callers.'
                )
            yield self.session

    async def reflect_batch(
        self, requests: list[ReflectionRequest]
    ) -> tuple[list[MentalModel], list[UUID], list[UUID]]:
        """Run reflection for multiple entities in parallel, grouped by vault_id.

        Returns ``(models, abandoned_entity_ids, failed_entity_ids)``:

        * ``models`` is the list of MentalModels whose Phase 5 CAS UPDATE
          applied.
        * ``abandoned_entity_ids`` is entities whose CAS UPDATE abandoned
          (a concurrent worker advanced the row's version between our
          read and write) — the caller routes them through the queue
          layer's abandon path (no retry_count increment).
        * ``failed_entity_ids`` is entities that raised a non-cancellation
          exception during reflection (LM circuit open, DB hiccup,
          extraction error, etc.) — the caller routes them through
          ``mark_failed`` so retry_count increments toward DEAD_LETTER
          and operators get visibility. Conflating these with
          ``abandoned_entity_ids`` would silently carousel a broken
          entity between PENDING/PROCESSING forever.
        """
        if not requests:
            return [], [], []

        # Dedupe by entity_id so the queue routing in the service layer
        # doesn't race on a single queue row (a duplicate would otherwise
        # produce two competing mark_abandoned / mark_failed UPDATEs).
        # Keep the first request per entity_id; this preserves the
        # caller's intent for per-request limits when distinct requests
        # for the same entity collide in one batch.
        seen_entities: set[UUID] = set()
        deduped: list[ReflectionRequest] = []
        for r in requests:
            if r.entity_id in seen_entities:
                continue
            seen_entities.add(r.entity_id)
            deduped.append(r)
        requests = deduped

        abandoned_entity_ids: list[UUID] = []
        failed_entity_ids: list[UUID] = []

        # 1. Group by Vault ID to optimize DB fetching
        vault_groups = defaultdict(list)
        for r in requests:
            vault_groups[r.vault_id].append(r)

        logger.info(
            f'Starting batch reflection for {len(requests)} entities across {len(vault_groups)} vaults'
        )

        all_success_models = []
        concurrency = self.config.server.memory.reflection.max_concurrency
        sem = asyncio.Semaphore(concurrency)

        for vault_id, v_requests in vault_groups.items():
            entity_ids = [r.entity_id for r in v_requests]

            # 1.1 Batch Load Data for this Vault (Serial DB Access).
            # This is the one-shot orchestrator-level read; uses self.session.
            # All subsequent per-entity DB work opens its own short session
            # via ``_entity_session`` (V18-c) so transactions don't span
            # the per-entity LLM phases.
            models_map = await self._batch_get_or_create_models(entity_ids, vault_id=vault_id)
            entities_map = await self._batch_get_entities(entity_ids)
            # Batch fetch uses the most-permissive limit in the group;
            # per-request slicing in _process_entity_reflection trims down.
            batch_limit = (
                None
                if any(r.limit_recent_memories is None for r in v_requests)
                else max(
                    r.limit_recent_memories
                    for r in v_requests
                    if r.limit_recent_memories is not None
                )
            )
            memories_map = await self._batch_fetch_recent_memories(
                entity_ids, vault_id=vault_id, limit_per_entity=batch_limit
            )

            # 1.2 Commit any newly-created MentalModel rows so the per-entity
            # Phase 5 CAS UPDATE can find them. Without this commit, the rows
            # added by _batch_get_or_create_models live only in self.session's
            # pending queue; a per-entity session running on a separate
            # connection would not see them, and Phase 5's
            # WHERE id=:id AND version=0 predicate would match zero rows,
            # silently abandoning every brand-new entity's reflection.
            # A commit with no pending state is a no-op in SQLAlchemy
            # asyncio mode, not an error — so a bare commit is safe even
            # when every entity in this batch already had its row.
            #
            # If the commit raises (e.g. IntegrityError on a duplicate
            # (entity_id, vault_id), OperationalError on a connection drop,
            # serialisation failure), the exception propagates up to the
            # caller and aborts this batch. Swallowing it silently would
            # leave the batch's Phase 5 CAS UPDATEs targeting non-existent
            # rows and abandoning every reflection without a log line —
            # exactly the failure mode the round-1 fix had to prevent.
            await self.session.commit()

            # Expunge each MentalModel from self.session so any in-memory
            # attribute mutations made by the per-entity phase methods on
            # these instances cannot flow into a second UPDATE via
            # dirty-tracking — Phase 5's per-entity CAS is the
            # authoritative write path, and a second un-versioned UPDATE
            # would silently overwrite concurrent writes. Only the
            # MentalModel instances are expunged; entities, units, and
            # other test-loaded state stay attached to self.session.
            for mm in models_map.values():
                if mm in self.session:
                    self.session.expunge(mm)

            # 1.3 Concurrent Processing for this Vault.
            # ``return_exceptions=True`` ensures that a BaseException in
            # one coroutine (e.g. CancelledError on shutdown) does NOT
            # tear down sibling coroutines mid-flight with partially
            # populated abandoned/failed lists. Normal exceptions are
            # already trapped inside _process_entity_reflection and
            # routed into ``failed_entity_ids`` explicitly.
            results = await asyncio.gather(
                *[
                    self._process_entity_reflection(
                        req,
                        models_map,
                        entities_map,
                        memories_map,
                        sem,
                        abandoned_entity_ids,
                        failed_entity_ids,
                    )
                    for req in v_requests
                ],
                return_exceptions=True,
            )
            for req, outcome in zip(v_requests, results, strict=True):
                if isinstance(outcome, MentalModel):
                    all_success_models.append(outcome)
                elif isinstance(outcome, BaseException):
                    # A BaseException escaped the inner handler — log and
                    # route to failed so the queue layer increments
                    # retry_count and surfaces the failure.
                    logger.error(
                        'Reflection BaseException for entity %s: %r',
                        req.entity_id,
                        outcome,
                    )
                    if req.entity_id not in failed_entity_ids:
                        failed_entity_ids.append(req.entity_id)

        # No batch-end commit: Phase 5's per-entity CAS UPDATE is the only
        # write path for ``mental_models``, and Phase 6 commits its own
        # writes inside the per-entity session. Any in-memory attribute
        # mutations made on detached MentalModel instances during the
        # LLM phases are intentionally not flushed — they exist only for
        # the caller's return-value convenience and do not represent
        # pending DB state.
        return all_success_models, abandoned_entity_ids, failed_entity_ids

    async def _process_entity_reflection(
        self,
        req: ReflectionRequest,
        models_map: dict[UUID, MentalModel],
        entities_map: dict[UUID, Entity],
        memories_map: dict[UUID, list[MemoryUnit]],
        sem: asyncio.Semaphore,
        abandoned_entity_ids: list[UUID],
        failed_entity_ids: list[UUID],
    ) -> MentalModel | None:
        """Process a single entity reflection with semaphore control.

        Returns:
            * ``MentalModel`` on success (Phase 5 CAS UPDATE applied).
            * ``None`` when the Phase 5 CAS UPDATE abandoned (concurrent
              writer won the version race) — entity_id is appended to
              ``abandoned_entity_ids``.
            * ``None`` when a non-cancellation exception was raised
              inside the per-entity pipeline — entity_id is appended to
              ``failed_entity_ids``. These two lists are disjoint:
              CAS abandons must NOT enter the failure path because the
              queue's ``mark_failed`` increments retry_count and a
              perpetually-contended entity would hit DEAD_LETTER.
        """
        eid = req.entity_id
        async with sem:
            try:
                entity = entities_map.get(eid)
                # Per-request slice: batch fetch is already capped at MAX_FULL_SCOPE_UNITS
                # by SQL; honour each request's own limit here.
                fetched = memories_map.get(eid, [])
                recent_memories = (
                    fetched
                    if req.limit_recent_memories is None
                    else fetched[: req.limit_recent_memories]
                )
                outcome = await self._reflect_entity_internal(
                    entity_id=eid,
                    mental_model=models_map[eid],
                    entity=entity,
                    recent_memories=recent_memories,
                    vault_id=req.vault_id,
                )
                if outcome is None:
                    # CAS abandon — caller re-enqueues via mark_abandoned
                    # (no retry_count increment) instead of mark_failed
                    # (counts toward DEAD_LETTER).
                    abandoned_entity_ids.append(eid)
                return outcome
            except Exception as e:
                # Real failure (LM circuit-broken, DB OperationalError,
                # extraction crash). Routing this through the abandon
                # path would silently carousel the entity between
                # PENDING and PROCESSING forever. The failed list is
                # routed through mark_failed at the service layer.
                logger.error(f'Reflection failed for entity {eid}: {e}', exc_info=True)
                failed_entity_ids.append(eid)
                return None

    async def _reflect_entity_internal(
        self,
        entity_id: UUID,
        mental_model: MentalModel,
        entity: Entity | None,
        recent_memories: list[MemoryUnit],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> MentalModel | None:
        """Internal logic for single entity reflection, decoupled from DB fetching logic.

        Returns ``None`` when the Phase 5 CAS UPDATE abandoned because a
        concurrent worker advanced the ``mental_models.version`` between
        our read and write. The caller (engine.reflect_batch) filters
        None out of ``all_success_models``, so the service layer's
        ``mark_failed`` re-enqueues the entity for a fresh attempt on
        the next scheduler tick. Returning the in-memory model here
        would route the entity through ``complete_reflection``, which
        DELETES the queue row — the work would be lost until some
        unrelated path re-enqueued the entity, contradicting the
        design-doc invariant that says "next tick retries."
        """
        entity_name = entity.canonical_name if entity else 'Unknown'
        entity_type = entity.entity_type if entity else None

        # Intra-worker per-entity serialization: two coroutines reflecting on
        # the same entity within this process serialize on the same
        # asyncio.Lock and run sequentially. Cross-worker dedup is handled by
        # the reflection queue's SELECT FOR UPDATE SKIP LOCKED claim, so this
        # lock does not need to be visible across processes. Phase 5's CAS
        # UPDATE protects against any cross-process race that slips through
        # the queue — the loser of the version race abandons cleanly.
        entity_lock = await get_entity_lock(entity_id)
        async with entity_lock:
            with trace_span(
                'memex.reflection',
                'reflection',
                {
                    'reflection.entity_id': str(entity_id),
                    'reflection.entity_name': entity_name,
                    'reflection.vault_id': str(vault_id),
                },
            ):
                # Phase 0: Update Existing — opens its own short DB tx
                # internally; no transaction held during the LLM call.
                # Returns (observations, mutated) so the no-memories early
                # return can detect whether anything changed and decide
                # whether to persist via Phase 5 CAS.
                updated_observations, phase0_mutated = await self._phase_0_update(
                    mental_model, entity_name, recent_memories, vault_id=vault_id
                )

                if not recent_memories:
                    # No new memories. If Phase 0 mutated observations
                    # (pruned evidence or LLM updates), persist via CAS so
                    # the change doesn't get lost (pre-V18 the orchestrator
                    # batch commit handled it). Otherwise it's a true no-op
                    # — return without touching the DB.
                    if phase0_mutated:
                        # Preserve the prior entity_metadata description on
                        # this prune-only path — Phase 4 was not run, so we
                        # don't have a fresh ``entity_summary`` to use.
                        # Passing ``entity_summary=''`` here would clobber
                        # the description built by the most recent full
                        # reflection cycle, which is a real regression.
                        prior_meta = mental_model.entity_metadata or {}
                        prior_summary = prior_meta.get('description', '') or ''
                        prior_type = prior_meta.get('category') or entity_type
                        async with self._entity_session() as ph5_session:
                            applied = await self._phase_5_finalize(
                                mental_model,
                                updated_observations,
                                session=ph5_session,
                                entity_summary=prior_summary,
                                entity_type=prior_type,
                            )
                        if not applied:
                            # Prune lost the CAS race. Surface as failure so
                            # the queue layer re-enqueues for retry instead
                            # of deleting the row.
                            return None
                    return mental_model

                # Phase 1: Seed (pure LLM — no DB)
                candidates = await self._phase_1_seed(
                    recent_memories, entity_name, updated_observations, vault_id=vault_id
                )

                # Phase 2: Hunt — opens its own short DB tx for the vector
                # search + unit hydration; releases it before returning.
                candidates_with_evidence = await self._phase_2_hunt(candidates, vault_id=vault_id)

                # Phase 3: Validate (pure LLM — no DB)
                validated = await self._phase_3_validate(
                    candidates_with_evidence, vault_id=vault_id
                )

                # Phase 4: Compare (pure LLM — no DB)
                final_obs, entity_summary = await self._phase_4_compare(
                    updated_observations,
                    validated,
                    vault_id=vault_id,
                    entity_name=entity_name,
                )

                # Phase 5: Finalize Model (CAS UPDATE — may abandon on version
                # conflict). Opens its own short DB tx and commits per-entity.
                async with self._entity_session() as ph5_session:
                    applied = await self._phase_5_finalize(
                        mental_model,
                        final_obs,
                        session=ph5_session,
                        entity_summary=entity_summary,
                        entity_type=entity_type,
                    )
                if not applied:
                    # Another worker refreshed this entity while our LLM
                    # phases ran. Return None so engine.reflect_batch
                    # excludes us from ``all_success_models`` and the
                    # service layer routes us to ``mark_failed`` (which
                    # flips the queue row back to FAILED so the next
                    # scheduler tick re-claims it via SKIP LOCKED) instead
                    # of ``complete_reflection`` (which would DELETE the
                    # queue row, losing the retry).
                    return None

                # Phase 6: Enrich (Memory Evolution) — opens its own short DB
                # tx for the unit-metadata writes; releases it before any
                # follow-on LLM call.
                if self.config.server.memory.reflection.enrichment_enabled:
                    await self._phase_6_enrich(
                        entity_name=entity_name,
                        entity_summary=entity_summary,
                        final_obs=final_obs,
                        recent_memories=recent_memories,
                        vault_id=vault_id,
                    )

                return mental_model

    async def _phase_5_finalize(
        self,
        mental_model: MentalModel,
        final_obs: list[Observation],
        session: AsyncSession | None = None,
        entity_summary: str = '',
        entity_type: str | None = None,
    ) -> bool:
        """Phase 5: CAS UPDATE on mental_models, version-checked.

        Issues a single ``UPDATE mental_models ... WHERE id = :id AND
        version = :claimed_version`` and bumps the version atomically.
        If another worker has refreshed this entity since the version
        was read, the WHERE clause matches zero rows and this reflection
        is abandoned (returns ``False``); the in-memory ``mental_model``
        is left unchanged so the caller doesn't surface stale state.
        The next scheduler tick will re-run reflection on the fresher
        state.

        On success, the in-memory ``mental_model`` is mutated to match
        what was written and ``session.commit()`` is called so the
        write is visible to other transactions before this function
        returns. ``True`` is returned.

        ``session`` may be ``None`` for legacy callers (unit tests with
        a mocked engine session); in that case the orchestrator's
        ``self.session`` is used directly (single-session semantics).
        Production callers pass a per-entity session from
        ``_entity_session()`` so the CAS UPDATE runs in its own short
        transaction without spanning any LLM I/O.

        The CAS UPDATE is its own atomic SQL statement — no asyncio.Lock
        is needed to serialize concurrent writes to the row; Postgres'
        row-level locking handles that.
        """
        active_session = session if session is not None else self.session
        claimed_version = mental_model.version
        new_observations = [obs.model_dump(mode='json') for obs in final_obs]
        new_entity_metadata = {
            'description': entity_summary,
            'category': entity_type,
            'observation_count': len(final_obs),
        }

        obs_text = ' '.join([f'{o.title} - {o.content}' for o in final_obs])
        full_text = format_for_embedding(
            text=obs_text,
            fact_type='observation',
            context=mental_model.name,
        )
        embedding_list = await self._async_encode([full_text])
        new_embedding = embedding_list[0]
        now = datetime.now(timezone.utc)

        stmt = (
            sa_update(MentalModel)
            .where(MentalModel.id == mental_model.id)  # type: ignore[arg-type]
            .where(MentalModel.version == claimed_version)  # type: ignore[arg-type]
            .values(
                observations=new_observations,
                entity_metadata=new_entity_metadata,
                version=MentalModel.version + 1,
                last_refreshed=now,
                embedding=new_embedding,
            )
            # Skip ORM synchronize: callers may still hold the loaded
            # MentalModel instance and shouldn't have its attributes
            # silently expired by a Core UPDATE — the success path
            # mutates the in-memory model explicitly below.
            .execution_options(synchronize_session=False)
        )
        result = await active_session.execute(stmt)
        rowcount = getattr(result, 'rowcount', 0) or 0
        if rowcount == 0:
            REFLECTION_CAS_ABANDONS_TOTAL.inc()
            logger.warning(
                'CAS abandon for mental model %s (entity %s): version=%d (concurrent refresh won; '
                'next scheduler tick will retry)',
                mental_model.id,
                mental_model.entity_id,
                claimed_version,
            )
            return False

        # Commit the per-entity session so the write is visible to other
        # transactions before we return. When ``session`` was passed in,
        # the caller's `async with self._entity_session()` block will see
        # this committed state on exit. When the orchestrator's
        # ``self.session`` is used (legacy single-session path), the
        # commit closes the implicit autobegin transaction.
        await active_session.commit()

        mental_model.observations = new_observations
        mental_model.entity_metadata = new_entity_metadata
        mental_model.version = claimed_version + 1
        mental_model.last_refreshed = now
        mental_model.embedding = new_embedding
        return True

    async def _refresh_observation(self, item: 'ReflectionQueue') -> None:
        """Surgically refresh one observation after an MU deprio.

        Three-phase shape — no DB transaction spans the LLM call, matching
        the V18 invariant ("per-entity sessions release tx between phases"):

          Phase A (short read tx)
            Load the MentalModel for ``(item.entity_id, item.vault_id)``.
            Locate the observation by ``obs.id == item.observation_id``;
            if absent, idempotent ack via
            ``REFRESH_OBSERVATION_TASK_OBS_ALREADY_PRUNED_TOTAL``.
            Race re-check: query every sibling refresh-task row for the
            same ``(entity, vault, obs)`` and read every ``source_unit_id``.
            If NONE of those triggering MUs is still cited by
            ``obs.evidence``, the deprio signal has already been absorbed;
            idempotent ack via
            ``REFRESH_OBSERVATION_TASK_ALREADY_ABSORBED_TOTAL``.
            Compute ``live_ids`` = evidence MUs whose
            ``is_deprioritized = False`` AND ``vault_id IN (item.vault_id,
            GLOBAL_VAULT_ID)``. ``status`` is intentionally NOT filtered:
            STALE evidence (note-supersession cascade) remains cited as
            historical support — the historical-citation question was
            deferred and STALE evidence is the de-facto audit trail.
            Snapshot ``claimed_version``, the observation dict, the obs
            index, and the surviving units into pure-Python state. Close
            the session (releases its DB conn back to the pool).

          Phase B (no DB session, no lock)
            If ``live_ids`` is empty, the decision is `drop` — skip LLM.
            Otherwise invoke ``RefreshObservationSignature`` to restate
            content on the surviving evidence. The LLM call runs with NO
            database transaction open and NO advisory/asyncio lock held.

          Phase C (short write tx)
            CAS UPDATE: ``WHERE id = mm.id AND version = claimed_version``.
            ``rowcount = 0`` ⇒ Phase 5 or another refresh advanced the
            version between Phase A and Phase C; raise
            ``AdvisoryLockTakenError`` so the scheduler re-claims with
            backoff WITHOUT bumping retry_count — concurrent contention is
            not a failure. Apply the LLM's ``should_drop`` decision with
            a retention guardrail: when
            ``len(live_ids) >= min_evidence_for_obs_retention``, override
            ``should_drop=True`` and keep the restated content.

        Embedding stays on the refresh path — centroid drift is dominated
        by full reflect; the next routine cycle recomputes it.
        """
        from memex_core.memory.sql_models import (
            MemoryUnit,
            MentalModel,
            ReflectionQueue,
        )

        start = datetime.now(timezone.utc)
        try:
            # ---------- Phase A: short read tx ----------
            mm_id: UUID
            mm_entity_id: UUID
            claimed_version: int
            original_observations: list[Any]
            obs: dict[str, Any]
            obs_index: int
            obs_id_str: str
            evidence_list: list[Any]
            live_ids: set[UUID]
            obs_context: ReflectObservationContext | None = None
            surviving_context: list[ReflectMemoryContext] = []

            async with self._entity_session() as session:
                mm_stmt = (
                    select(MentalModel)
                    .where(col(MentalModel.entity_id) == item.entity_id)
                    .where(col(MentalModel.vault_id) == item.vault_id)
                )
                mm_result = await session.exec(mm_stmt)
                mm = mm_result.first()
                if mm is None:
                    REFRESH_OBSERVATION_TASK_OBS_ALREADY_PRUNED_TOTAL.inc()
                    return

                claimed_version = mm.version
                mm_id = mm.id
                mm_entity_id = mm.entity_id
                obs_id_str = str(item.observation_id)
                obs_found: dict[str, Any] | None = None
                obs_index_found: int | None = None
                original_observations = list(mm.observations or [])
                # JSONB-loaded observations are dicts in production. Phase 4
                # reconstruction can transit ``Observation`` instances; the
                # downstream CAS write needs dicts, so coerce via model_dump
                # when present. Defensive — should not fire in steady state.
                for i, candidate in enumerate(original_observations):
                    if isinstance(candidate, dict):
                        if str(candidate.get('id')) == obs_id_str:
                            obs_found = candidate
                            obs_index_found = i
                            break
                    else:
                        cand_id = getattr(candidate, 'id', None)
                        dump = getattr(candidate, 'model_dump', None)
                        if str(cand_id) == obs_id_str and callable(dump):
                            obs_found = dump(mode='json')
                            obs_index_found = i
                            break
                if obs_found is None or obs_index_found is None:
                    REFRESH_OBSERVATION_TASK_OBS_ALREADY_PRUNED_TOTAL.inc()
                    return
                obs = obs_found
                obs_index = obs_index_found

                # Sibling refresh-task lookup: read every IN-FLIGHT row's
                # source_unit_id so the "already absorbed" check considers
                # all triggering MUs from the deprio burst. Historical
                # FAILED/DEAD_LETTER/completed-but-not-deleted rows are
                # filtered out — their source MUs may have been re-cited
                # since and shouldn't make the absorption check too strict.
                #
                # Trade-off: if a prior refresh for this observation went to
                # DEAD_LETTER and the deprio'd MU is still cited in
                # ``obs.evidence``, this task will proceed through the LLM
                # call to re-drop it. That's redundant work but self-
                # correcting: the CAS write replaces the observation in-
                # place. The alternative (include DEAD_LETTER rows) would
                # over-absorb when source MUs have been legitimately re-cited.
                from memex_core.memory.sql_models import ReflectionStatus

                # ``source_unit_id IS NOT NULL`` is intentional: rows with
                # NULL ``source_unit_id`` carry no signal for the "already
                # absorbed" check — they didn't originate from a specific
                # deprio'd MU we can probe against. The current production
                # paths (``_flip_deprioritized``, ``flush_deferred_observation_refresh``)
                # ALWAYS set ``source_unit_id`` before enqueuing, so a NULL
                # row would be an invariant violation. Excluding them keeps
                # the absorption check from being relaxed by malformed rows
                # rather than tightened; the explicit fallback on the next
                # lines covers the legitimate "this is the only triggering
                # row" case via ``item.source_unit_id``.
                sibling_stmt = (
                    select(ReflectionQueue.source_unit_id)
                    .where(col(ReflectionQueue.entity_id) == item.entity_id)
                    .where(col(ReflectionQueue.vault_id) == item.vault_id)
                    .where(col(ReflectionQueue.observation_id) == item.observation_id)
                    .where(col(ReflectionQueue.task_type) == 'refresh_observation')
                    .where(col(ReflectionQueue.source_unit_id).is_not(None))
                    .where(
                        col(ReflectionQueue.status).in_(
                            [ReflectionStatus.PENDING, ReflectionStatus.PROCESSING]
                        )
                    )
                )
                sibling_result = await session.exec(sibling_stmt)
                triggering_unit_ids: set[str] = {
                    str(sid) for sid in sibling_result.all() if sid is not None
                }
                if not triggering_unit_ids and item.source_unit_id is not None:
                    triggering_unit_ids = {str(item.source_unit_id)}

                evidence_list = obs.get('evidence') or []
                cited_mu_strs = {
                    str(e.get('memory_id'))
                    for e in evidence_list
                    if isinstance(e, dict) and e.get('memory_id') is not None
                }
                if triggering_unit_ids and not (triggering_unit_ids & cited_mu_strs):
                    REFRESH_OBSERVATION_TASK_ALREADY_ABSORBED_TOTAL.inc()
                    return

                evidence_uuids: list[UUID] = []
                for e in evidence_list:
                    if not isinstance(e, dict):
                        continue
                    mid = e.get('memory_id')
                    if mid is None:
                        continue
                    try:
                        evidence_uuids.append(UUID(str(mid)))
                    except (ValueError, TypeError):
                        continue

                live_ids = set()
                if evidence_uuids:
                    live_stmt = select(MemoryUnit.id).where(
                        col(MemoryUnit.id).in_(evidence_uuids),
                        col(MemoryUnit.is_deprioritized).is_(False),
                        col(MemoryUnit.vault_id).in_([item.vault_id, GLOBAL_VAULT_ID]),
                    )
                    live_result = await session.exec(live_stmt)
                    live_ids = set(live_result.all())

                if live_ids:
                    # Load full units inside the read session so we can build
                    # the LLM context before the session closes — the LLM call
                    # itself happens in Phase B with NO session open.
                    live_units_stmt = select(MemoryUnit).where(
                        col(MemoryUnit.id).in_(list(live_ids)),
                        col(MemoryUnit.vault_id).in_([item.vault_id, GLOBAL_VAULT_ID]),
                    )
                    live_units_result = await session.exec(live_units_stmt)
                    surviving_units = list(live_units_result.all())
                    obs_context = ReflectObservationContext(
                        index_id=0,
                        title=str(obs.get('title') or ''),
                        content=str(obs.get('content') or ''),
                    )
                    surviving_context = build_memory_context(surviving_units)
            # ---------- Phase A end: session closed, no lock held ----------

            # ---------- Phase B: LLM call (no DB tx, no lock) ----------
            refreshed: RefreshedObservation | None = None
            if live_ids and obs_context is not None:
                try:
                    refreshed = await self._invoke_refresh_signature_with_context(
                        obs_context, surviving_context
                    )
                except (RuntimeError, ValueError) as e:
                    # Wrap with the observation context so production logs
                    # can pin a sporadic LLM failure to a specific obs.
                    raise RuntimeError(
                        f'refresh signature failed for observation '
                        f'{obs_id_str} (entity {mm_entity_id}): {e}'
                    ) from e

            # ---------- Phase C: short write tx + CAS UPDATE ----------
            reflect_cfg = self.config.server.memory.reflection
            min_retention = reflect_cfg.min_evidence_for_obs_retention

            should_drop = not live_ids  # zero-evidence ⇒ drop
            zero_evidence_drop = should_drop
            llm_drop_honored = False
            llm_drop_overridden = False
            empty_content_coerced = False
            if refreshed is not None:
                llm_drop = bool(refreshed.should_drop)
                # Defensive: if the validator was bypassed (DSPy adapter swallowed
                # the ValueError) we still receive should_drop=False with empty
                # content/title. Treat as a drop and skip the retention guardrail
                # — empty payload is unrecoverable regardless of evidence count.
                # Tracked under its own counter so operators can distinguish
                # validator-bypass coercions from honored LLM drops.
                content_empty = not (
                    (refreshed.content or '').strip() and (refreshed.title or '').strip()
                )
                if not llm_drop and content_empty:
                    logger.warning(
                        'refresh_observation: refreshed payload had empty content/title '
                        'but should_drop=False; coercing to drop'
                    )
                    llm_drop = True
                    empty_content_coerced = True
                if llm_drop and not content_empty and len(live_ids) >= min_retention:
                    logger.warning(
                        'refresh_observation: LLM should_drop=True overridden; '
                        'live_ids=%d, reason=%r',
                        len(live_ids),
                        refreshed.dropped_reason,
                    )
                    # Defer counter bump until AFTER CAS commit succeeds —
                    # on CAS abandon we re-run Phase C on the next claim
                    # and would otherwise double-count the override.
                    llm_drop_overridden = True
                    llm_drop = False
                if llm_drop:
                    should_drop = True
                    # Only attribute to LLM_drop_honored when the LLM actually
                    # set should_drop=True. Empty-content coercion uses its
                    # own counter (see EMPTY_CONTENT_COERCED below).
                    if not empty_content_coerced:
                        llm_drop_honored = True

            # Build the new observations list as a pure-Python operation.
            if should_drop:
                new_observations = [
                    o
                    for o in original_observations
                    if str(o.get('id') if isinstance(o, dict) else getattr(o, 'id', None))
                    != obs_id_str
                ]
            elif refreshed is not None:
                live_strs = {str(u) for u in live_ids}
                new_evidence = [
                    e
                    for e in evidence_list
                    if isinstance(e, dict) and str(e.get('memory_id')) in live_strs
                ]
                new_obs = dict(obs)
                new_obs['content'] = refreshed.content
                new_obs['title'] = refreshed.title
                new_obs['evidence'] = new_evidence
                new_observations = list(original_observations)
                new_observations[obs_index] = new_obs
            else:
                # Unreachable: should_drop=False implies refreshed is not None.
                new_observations = list(original_observations)

            now = datetime.now(timezone.utc)
            async with self._entity_session() as session:
                # Re-validate live_ids inside the write tx. Without this, a
                # concurrent _flip_deprioritized(unit_X) committed during
                # Phase B would have its refresh-task enqueue DEDUPED away
                # by the partial UNIQUE (our row is still PROCESSING), and
                # we'd then commit observations still citing the now-deprio'd
                # X — the exact deprio leak this feature closes. Abandoning
                # the cycle (raise → reclaim without retry bump) is the
                # correct response: the re-run reads the current state.
                if evidence_uuids:
                    revalidate_stmt = select(MemoryUnit.id).where(
                        col(MemoryUnit.id).in_(evidence_uuids),
                        col(MemoryUnit.is_deprioritized).is_(False),
                        col(MemoryUnit.vault_id).in_([item.vault_id, GLOBAL_VAULT_ID]),
                    )
                    revalidate_result = await session.exec(revalidate_stmt)
                    current_live_ids = set(revalidate_result.all())
                    if current_live_ids != live_ids:
                        REFLECTION_CAS_ABANDONS_TOTAL.inc()
                        raise RefreshStaleReadError(
                            f'refresh live-evidence changed between Phase A and '
                            f'Phase C for entity {mm_entity_id}: phase_a='
                            f'{len(live_ids)}, current={len(current_live_ids)}; '
                            'reclaim'
                        )

                cas_stmt = (
                    sa_update(MentalModel)
                    .where(col(MentalModel.id) == mm_id)
                    .where(col(MentalModel.version) == claimed_version)
                    .values(
                        observations=new_observations,
                        version=claimed_version + 1,
                        last_refreshed=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                cas_result = await session.execute(cas_stmt)
                rowcount = getattr(cas_result, 'rowcount', 0) or 0
                if rowcount == 0:
                    REFLECTION_CAS_ABANDONS_TOTAL.inc()
                    raise RefreshCASAbandonedError(
                        f'refresh CAS abandoned for entity {mm_entity_id} '
                        f'(version {claimed_version} advanced concurrently); reclaim'
                    )
                await session.commit()
            # Counters only on successful CAS commit. On CAS abandon, none
            # of these fire — the re-claim will re-decide and re-tick.
            if zero_evidence_drop:
                REFRESH_OBSERVATION_TASK_ZERO_EVIDENCE_TOTAL.inc()
            elif empty_content_coerced:
                REFRESH_OBSERVATION_EMPTY_CONTENT_COERCED_TOTAL.inc()
            elif llm_drop_honored:
                REFRESH_OBSERVATION_TASK_DROPPED_BY_LLM_TOTAL.inc()
            elif llm_drop_overridden:
                REFRESH_OBSERVATION_DROP_OVERRIDDEN_TOTAL.inc()
            REFRESH_OBSERVATION_TASK_COMPLETED_TOTAL.inc()
        finally:
            # Latency includes claim-to-outcome wall clock for ALL paths,
            # including CAS abandons and ``AdvisoryLockTakenError`` raises.
            # Intentional — operators care about end-to-end latency under
            # contention, not just the happy path. Filter by counter ratios
            # if a CAS-abandon-only or happy-path-only view is needed.
            REFRESH_OBSERVATION_TASK_LATENCY_SECONDS.observe(
                (datetime.now(timezone.utc) - start).total_seconds()
            )

    async def _invoke_refresh_signature_with_context(
        self,
        obs_context: ReflectObservationContext,
        surviving_context: list[ReflectMemoryContext],
    ) -> RefreshedObservation:
        """Invoke RefreshObservationSignature with pre-built contexts.

        Contexts are built inside Phase A (read tx) so the LLM call here
        runs with NO database session open and NO advisory lock held.
        Raises ``RuntimeError`` on transient LLM failure or malformed
        output. The scheduler's per-item handler catches this and calls
        ``mark_queue_item_failed`` — retry_count is incremented and the
        row is re-claimed on the next tick (DEAD_LETTERs after
        max_retries).
        """
        if self.lm is None:
            raise RuntimeError('LM must be initialized')
        predictor = dspy.Predict(RefreshObservationSignature)
        result = await run_dspy_operation(
            lm=self.lm,
            predictor=predictor,
            input_kwargs={
                'observation': obs_context,
                'surviving_evidence': surviving_context,
            },
            operation_name='reflection.refresh_observation',
        )
        if result is None:
            raise RuntimeError(
                'RefreshObservationSignature: transient LLM failure '
                '(run_dspy_operation returned None)'
            )
        refreshed = getattr(result, 'refreshed', None)
        if refreshed is None:
            raise RuntimeError(
                'RefreshObservationSignature: malformed LLM output (missing refreshed field)'
            )
        return refreshed

    async def _phase_6_enrich(
        self,
        entity_name: str,
        entity_summary: str,
        final_obs: list[Observation],
        recent_memories: list[MemoryUnit],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> None:
        """Phase 6: Push enriched tags from mental model back into evidence units.

        Opens a single per-entity session for the duration of the phase.
        The read transaction is committed before the LLM call so no DB
        transaction is held during DSPy I/O; the same session is reused
        for the write transaction afterwards. Connection is held for the
        duration of this entity's enrichment but only for *one entity*,
        not the whole batch — V18-c's per-entity session is what keeps
        the connection pool from being saturated across many concurrent
        reflections.
        """
        if not final_obs:
            return

        # 1. Collect evidence unit IDs from all observations (preserve insertion order)
        evidence_ids: dict[UUID, None] = {}
        for obs in final_obs:
            for ev in obs.evidence:
                if ev.memory_id:
                    evidence_ids[ev.memory_id] = None

        if not evidence_ids:
            return

        async with self._entity_session() as ph6_session:
            # 2. Build unit map from recent_memories, load any missing from DB.
            # Exclude deprio'd MUs from both arms: the secondary fetch filters
            # ``is_deprioritized=False`` and the pre-existing map drops any
            # deprio'd unit ALREADY present in ``recent_memories`` — without
            # this, an in-memory MU flipped to deprio'd between Phase 0 and
            # Phase 6 would still get its tsvector/tags strengthened here.
            unit_map: dict[UUID, MemoryUnit] = {
                m.id: m for m in recent_memories if not m.is_deprioritized
            }
            missing_ids = set(evidence_ids.keys()) - set(unit_map.keys())

            if missing_ids:
                stmt = select(MemoryUnit).where(
                    col(MemoryUnit.id).in_(list(missing_ids)),
                    col(MemoryUnit.is_deprioritized).is_(False),
                )
                result = await ph6_session.exec(stmt)
                for unit in result.all():
                    unit_map[unit.id] = unit

            # 3. Filter to only units we have evidence for
            target_units = [unit_map[uid] for uid in evidence_ids if uid in unit_map]
            if not target_units:
                return

            # 4. Build LLM context
            obs_context = [
                ReflectObservationContext(index_id=i, title=o.title, content=o.content)
                for i, o in enumerate(final_obs)
            ]

            memory_context = []
            for i, unit in enumerate(target_units):
                meta = unit.unit_metadata or {}
                existing_tags = meta.get('enriched_tags', [])
                existing_kw = meta.get('enriched_keywords', [])
                all_existing = existing_tags + existing_kw
                tag_suffix = f' [tags: {", ".join(all_existing)}]' if all_existing else ''
                occurred = (unit.event_date or datetime.now(timezone.utc)).isoformat()
                memory_context.append(
                    ReflectMemoryContext(
                        index_id=i,
                        content=unit.text + tag_suffix,
                        occurred=occurred,
                    )
                )

            # 5. Release the read transaction before the LLM call so no
            # MVCC snapshot is pinned across DSPy I/O. A commit with no
            # active transaction (e.g. when recent_memories covered all
            # evidence and no DB read fired) is a no-op in SQLAlchemy
            # asyncio mode, not an error.
            await ph6_session.commit()

            # 6. Call LLM — session is open but no transaction held.
            enrich_predictor = dspy.Predict(EnrichmentSignature)

            if self.lm is None:
                raise RuntimeError('LM must be initialized for Phase 6')
            result = await run_dspy_operation(
                lm=self.lm,
                predictor=enrich_predictor,
                input_kwargs={
                    'entity_name': entity_name,
                    'entity_summary': entity_summary,
                    'observations': obs_context,
                    'memories': memory_context,
                },
                operation_name='reflection.enrich',
            )

            if not result or not result.enrichments:
                logger.info('Phase 6: No enrichments generated.')
                return

            # 7. Apply enrichments — new implicit transaction begins on first
            # DB op. Mutations to target_units (whether they came from
            # recent_memories or from the DB load in step 2) are flushed by
            # the commit below.
            now_iso = datetime.now(timezone.utc).isoformat()
            enriched_count = 0

            # First-pass: apply enrichments in-memory to ``target_units``.
            # We DO NOT issue merge() here — that step happens in a second
            # pass over a unit_id-sorted list so concurrent Phase 6 calls on
            # different entities sharing overlapping evidence units acquire
            # row locks in the same (id-ascending) order, eliminating the
            # cross-entity two-row deadlock pattern that would otherwise
            # leave one worker's reflection failing per cycle.
            units_to_merge: list[MemoryUnit] = []
            for enrichment in result.enrichments:
                idx = enrichment.memory_index
                if idx < 0 or idx >= len(target_units):
                    logger.warning(f'Phase 6: Invalid memory_index {idx}, skipping.')
                    continue

                unit = target_units[idx]
                if unit.unit_metadata is None:
                    unit.unit_metadata = {}

                # Set-union: accumulate tags across cycles
                existing_tags = set(unit.unit_metadata.get('enriched_tags', []))
                existing_kw = set(unit.unit_metadata.get('enriched_keywords', []))

                new_tags = existing_tags | {t.lower().strip() for t in enrichment.enriched_tags}
                new_kw = existing_kw | {k.lower().strip() for k in enrichment.enriched_keywords}

                unit.unit_metadata['enriched_tags'] = sorted(new_tags)
                unit.unit_metadata['enriched_keywords'] = sorted(new_kw)
                unit.unit_metadata['enriched_at'] = now_iso
                unit.unit_metadata['enriched_by_entity'] = entity_name

                flag_modified(unit, 'unit_metadata')
                units_to_merge.append(unit)
                enriched_count += 1

            # Direct UPDATE in id-ascending order — only ``metadata`` and
            # ``updated_at`` go in SET. ``merge()`` here used to copy every
            # mapped attribute from the source instance onto the persistent
            # copy, which marked ``search_tsvector`` dirty and forced it
            # into the UPDATE; Postgres rejects that because the column is
            # GENERATED ALWAYS AS STORED. The id-ascending UPDATE order
            # still gives deterministic row-lock acquisition, so concurrent
            # Phase 6 calls on overlapping evidence units cannot deadlock.
            now_ts = datetime.now(timezone.utc)
            for unit in sorted(units_to_merge, key=lambda u: u.id):
                stmt = (
                    sa_update(MemoryUnit)
                    .where(col(MemoryUnit.id) == unit.id)
                    .values(unit_metadata=unit.unit_metadata, updated_at=now_ts)
                )
                await ph6_session.exec(stmt)  # type: ignore[call-overload]

            await ph6_session.commit()

        # Clear the dirty flag on the orchestrator-session copy of each
        # mutated unit. ``recent_memories`` units are attached to
        # ``self.session``; ``flag_modified(unit, 'unit_metadata')``
        # above flagged them dirty there. Without this clear, the
        # orchestrator's downstream commit (in
        # services.reflection.reflect_batch via
        # queue_service.complete_reflection) would flush the same
        # mutation a second time on a different connection — without
        # Phase 6's id-ascending row lock ordering (opening a cross-
        # worker deadlock window on shared evidence units) and without a
        # version guard (last-writer-wins race that could roll back a
        # co-tenant worker's Phase 6 writes). The ph6_session UPDATE
        # above already produced the authoritative row in the DB.
        #
        # We use ``set_committed_value`` (not ``session.expire``):
        # expiring marks EVERY attribute as needing reload, so any
        # subsequent access (e.g. the next entity's reflection re-using
        # an overlapping evidence unit, reading ``m.is_deprioritized``
        # or ``m.occurred_start``) triggers SA's sync lazy-load path —
        # which can't await from async context and raises
        # ``MissingGreenlet``. ``set_committed_value`` only clears the
        # dirty bit on the single attribute we touched, leaving every
        # other loaded attribute intact.
        for unit in units_to_merge:
            try:
                if unit in self.session:
                    set_committed_value(unit, 'unit_metadata', unit.unit_metadata)
            except InvalidRequestError:
                pass

        logger.info(f'Phase 6: Enriched {enriched_count} memory units for entity "{entity_name}".')

    async def _batch_get_or_create_models(
        self, entity_ids: list[UUID], vault_id: UUID = GLOBAL_VAULT_ID
    ) -> dict[UUID, MentalModel]:
        """Fetch or create mental models for a batch of entities."""
        query = (
            select(MentalModel)
            .where(col(MentalModel.entity_id).in_(entity_ids))
            .where(col(MentalModel.vault_id) == vault_id)
        )

        results = (await self.session.exec(query)).all()
        models_map = {m.entity_id: m for m in results}

        missing_ids = set(entity_ids) - set(models_map.keys())
        if missing_ids:
            entities = await self._batch_get_entities(list(missing_ids))
            for eid in missing_ids:
                entity = entities.get(eid)
                name = entity.canonical_name if entity else 'Unknown'
                new_model = MentalModel(
                    entity_id=eid, name=name, observations=[], vault_id=vault_id
                )
                self.session.add(new_model)
                models_map[eid] = new_model

        return models_map

    async def _batch_get_entities(self, entity_ids: list[UUID]) -> dict[UUID, Entity]:
        query = select(Entity).where(col(Entity.id).in_(entity_ids))
        results = (await self.session.exec(query)).all()
        return {e.id: e for e in results}

    async def _batch_fetch_recent_memories(
        self,
        entity_ids: list[UUID],
        vault_id: UUID = GLOBAL_VAULT_ID,
        limit_per_entity: int | None = 20,
    ) -> dict[UUID, list[MemoryUnit]]:
        """Fetch recent memories for multiple entities via window function.

        Vault scoping: (vault_id == active OR vault_id == Global).
        limit_per_entity=None → unbounded fetch capped at MAX_FULL_SCOPE_UNITS.
        """

        effective_limit = MAX_FULL_SCOPE_UNITS if limit_per_entity is None else limit_per_entity

        # 1. Base query for units associated with these entities
        subq_base = (
            select(
                UnitEntity.entity_id,
                UnitEntity.unit_id,
                func.row_number()
                .over(
                    partition_by=col(UnitEntity.entity_id),
                    order_by=col(MemoryUnit.event_date).desc(),
                )
                .label('rn'),
            )
            .join(MemoryUnit, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .where(col(MemoryUnit.status) == ContentStatus.ACTIVE)
            .where(col(MemoryUnit.is_deprioritized).is_(False))
            .where(col(UnitEntity.entity_id).in_(entity_ids))
        )

        # 2. Apply Vault Filter (Fall-through)
        subq_base = subq_base.where(
            (col(MemoryUnit.vault_id) == vault_id) | (col(MemoryUnit.vault_id) == GLOBAL_VAULT_ID)
        )

        subq = subq_base.subquery()

        query = (
            select(MemoryUnit, subq.c.entity_id)
            .join(subq, col(subq.c.unit_id) == col(MemoryUnit.id))
            .where(subq.c.rn <= effective_limit)
            .options(defer(MemoryUnit.embedding))  # type: ignore
        )

        results = (await self.session.exec(query)).all()

        memories_map = defaultdict(list)
        for unit, eid in results:
            memories_map[eid].append(unit)

        # Variance-priority sort: concentrates LLM budget on uncertain units.
        # Gated on ReflectionConfig.variance_prioritisation_enabled (default False).
        # Stable sort preserves event_date DESC tiebreak from SQL.
        if self.config.server.memory.reflection.variance_prioritisation_enabled:
            for eid, units in memories_map.items():
                units.sort(key=_variance_key, reverse=True)

        return memories_map

    async def reflect_on_entity(self, request: ReflectionRequest) -> MentalModel:
        """Legacy wrapper for single entity reflection.

        Returns the applied MentalModel on success. Raises
        ``ReflectionAbandonedError`` (typed, surface adapters translate
        to a structured retry envelope) when the Phase 5 CAS UPDATE
        abandoned because a concurrent worker advanced the version.
        Raises ``RuntimeError`` only for true failures (exceptions in
        the engine path).

        The wrapper does NOT re-enqueue the queue row on abandon — the
        service layer handles queue routing when callers go through
        ``services.reflection.reflect_batch``.
        """
        results, abandoned, failed = await self.reflect_batch([request])
        if not results:
            if request.entity_id in abandoned:
                raise ReflectionAbandonedError(
                    f'Reflection for {request.entity_id} abandoned: '
                    'a concurrent worker advanced the mental_models.version '
                    'between read and Phase 5 CAS UPDATE. The entity has '
                    'NOT been re-enqueued by this wrapper — the service '
                    'layer handles re-enqueue when called via reflect_batch.'
                )
            # ``failed`` and the implicit "no models" fall-through both
            # land here. Either way the engine recorded a real failure
            # (or produced nothing for an unexpected reason); raising
            # RuntimeError signals the caller to route through
            # mark_failed at the service layer.
            raise RuntimeError(f'Reflection failed for {request.entity_id}')
        return results[0]

    async def _get_or_create_mental_model(
        self, entity_id: UUID, vault_id: UUID = GLOBAL_VAULT_ID
    ) -> MentalModel:
        query = (
            select(MentalModel)
            .where(col(MentalModel.entity_id) == entity_id)
            .where(col(MentalModel.vault_id) == vault_id)
        )

        result = await self.session.exec(query)
        model = result.first()

        if not model:
            entity = await self.session.get(Entity, entity_id)
            name = entity.canonical_name if entity else 'Unknown'

            model = MentalModel(entity_id=entity_id, name=name, observations=[], vault_id=vault_id)
            self.session.add(model)
            await self.session.commit()
            await self.session.refresh(model)

        return model

    async def _phase_0_update(
        self,
        model: MentalModel,
        entity_name: str,
        memories: list[MemoryUnit],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> tuple[list[Observation], bool]:
        """Phase 0: Update existing observations with new evidence; prune dead refs.

        Returns a tuple ``(observations, mutated)``. ``mutated`` is True if
        either the dead-ref prune (any evidence dropped, even from a single
        observation that retains other evidence) or the LLM update step
        changed the in-memory observation list versus the row in
        ``model.observations``. The caller uses this signal to decide
        whether to persist via Phase 5's CAS UPDATE on the no-recent-
        memories early-return path; on the main happy path Phase 5 always
        runs regardless.
        """
        current_observations = [Observation(**obs) for obs in model.observations]
        mutated = False
        if not current_observations:
            return current_observations, mutated

        # Prune evidence citing deleted memory units
        all_evidence_ids: set[UUID] = set()
        for obs in current_observations:
            for ev in obs.evidence:
                all_evidence_ids.add(ev.memory_id)

        if all_evidence_ids:
            # Per-entity DB session for the live-ID lookup. The session
            # closes before the LLM call below, so no transaction is held
            # during DSPy I/O. Phase 5's CAS UPDATE is the authoritative
            # write path for ``observations`` — Phase 0 just returns the
            # pruned ``current_observations`` upward; the caller decides
            # whether to persist via Phase 5 (e.g. the no-recent-memories
            # early return at ``_reflect_entity_internal`` does exactly
            # that when the prune changed the observation count).
            async with self._entity_session() as ph0_session:
                live_stmt = select(MemoryUnit.id).where(
                    col(MemoryUnit.id).in_(list(all_evidence_ids)),
                    col(MemoryUnit.is_deprioritized).is_(False),
                    (col(MemoryUnit.vault_id) == vault_id)
                    | (col(MemoryUnit.vault_id) == GLOBAL_VAULT_ID),
                )
                live_result = await ph0_session.exec(live_stmt)
                live_ids = set(live_result.all())
            dead_ids = all_evidence_ids - live_ids

            if dead_ids:
                pruned_to_empty: set[UUID] = set()
                for obs in current_observations:
                    original_len = len(obs.evidence)
                    obs.evidence = [ev for ev in obs.evidence if ev.memory_id not in dead_ids]
                    if len(obs.evidence) < original_len:
                        mutated = True
                        if not obs.evidence:
                            pruned_to_empty.add(obs.id)

                # Drop only observations pruned to empty
                if pruned_to_empty:
                    current_observations = [
                        obs for obs in current_observations if obs.id not in pruned_to_empty
                    ]

        if not current_observations or not memories:
            return current_observations, mutated

        memory_map = {i: m for i, m in enumerate(memories)}

        memory_context = build_memory_context(memories)

        obs_context = [
            ReflectObservationContext(index_id=i, title=o.title, content=o.content)
            for i, o in enumerate(current_observations)
        ]

        update_predictor = dspy.Predict(UpdateExistingSignature)

        if self.lm is None:
            raise RuntimeError('LM must be initialized')
        result = await run_dspy_operation(
            lm=self.lm,
            predictor=update_predictor,
            input_kwargs={'recent_memories': memory_context, 'existing_observations': obs_context},
            operation_name='reflection.update',
        )

        if not result or not result.updates:
            return current_observations, mutated

        for update in result.updates:
            if 0 <= update.observation_index < len(current_observations):
                obs = current_observations[update.observation_index]

                for new_ev in update.new_evidence:
                    mem_idx = new_ev.memory_id
                    if mem_idx is not None and mem_idx in memory_map:
                        mem = memory_map[mem_idx]
                        obs.evidence.append(
                            EvidenceItem(
                                memory_id=mem.id,
                                quote=new_ev.quote,
                                relevance=1.0,
                                explanation=new_ev.relevance_explanation,
                                timestamp=parse_timestamp(new_ev.timestamp),
                            )
                        )
                        mutated = True

                if update.has_contradiction:
                    note = update.contradiction_note or 'New evidence contradicts this observation.'
                    obs.content += f' [CONTRADICTION: {note}]'
                    mutated = True

        return current_observations, mutated

    async def _phase_1_seed(
        self,
        memories: list[MemoryUnit],
        topic: str,
        existing_obs: list[Observation],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> list[CandidateObservation]:
        """Phase 1: Generate candidate observations from recent memories."""
        if not memories:
            return []

        memory_context = build_memory_context(memories)

        obs_context = [
            ReflectObservationContext(index_id=i, title=o.title, content=o.content)
            for i, o in enumerate(existing_obs)
        ]

        seed_predictor = dspy.Predict(SeedPhaseSignature)

        if self.lm is None:
            raise RuntimeError('LM must be initialized')
        result = await run_dspy_operation(
            lm=self.lm,
            predictor=seed_predictor,
            input_kwargs={
                'memories_context': memory_context,
                'topic': topic,
                'existing_observations': obs_context,
            },
            operation_name='reflection.seed',
        )

        if result is None:
            raise RuntimeError('Phase 1 Seed failed (LLM returned None).')

        if not result.candidates:
            logger.warning('Phase 1 Seed returned no candidates.')
            return []

        return result.candidates

    async def _async_encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        loop = asyncio.get_running_loop()
        embeddings_np = await loop.run_in_executor(None, self.embedder.encode, texts)
        return [e.tolist() for e in embeddings_np]

    async def _phase_2_hunt(
        self,
        candidates: list[CandidateObservation],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> list[tuple[CandidateObservation, list[MemoryUnit]]]:
        """Phase 2: Retrieve evidence for candidates via vector search + tail sampling.

        Holds a single per-entity DB session for the duration of the search
        loop, then releases it. No DB transaction outlives this call.
        """
        from memex_core.memory.extraction import storage

        if not candidates:
            return []

        texts = [c.content for c in candidates]
        embeddings = await self._async_encode(texts)
        results: list[tuple[CandidateObservation, list[MemoryUnit]]] = []

        async with self._entity_session() as ph2_session:
            # 2b. Tail Sampling: Sample random memories from the vault
            tail_memories = await self._sample_tail_memories(ph2_session, vault_id=vault_id)

            for i, cand in enumerate(candidates):
                embedding = embeddings[i]

                similar_items = await storage.find_similar_facts(
                    ph2_session,
                    embedding,
                    limit=self.config.server.memory.reflection.search_limit,
                    threshold=self.config.server.memory.reflection.similarity_threshold,
                    vault_ids=[vault_id],
                    reflect_input_only=True,
                )

                if not similar_items:
                    # Even if no similar items, we still provide tail memories
                    results.append((cand, tail_memories))
                    continue

                unit_ids = [item[0] for item in similar_items]
                unit_stmt = (
                    select(MemoryUnit)
                    .where(col(MemoryUnit.id).in_(unit_ids))
                    .options(defer(MemoryUnit.embedding))  # type: ignore
                )
                units_result = await ph2_session.exec(unit_stmt)
                found_memories = list(units_result.all())

                # Similarity-based re-ranking
                similarity_map = {item[0]: item[1] for item in similar_items}
                found_memories.sort(
                    key=lambda m: similarity_map.get(m.id, 0.0),
                    reverse=True,
                )

                # Merge similar and tail memories (deduplicate by ID)
                all_mems = {m.id: m for m in found_memories}
                for tm in tail_memories:
                    if tm.id not in all_mems:
                        all_mems[tm.id] = tm

                results.append((cand, list(all_mems.values())))
        return results

    async def _sample_tail_memories(
        self, session: AsyncSession, vault_id: UUID
    ) -> list[MemoryUnit]:
        """Sample random memories from the vault to avoid echo chambers."""
        rate = self.config.server.memory.reflection.tail_sampling_rate
        if rate <= 0:
            return []

        # Heuristic: sample ~2-3 memories for default settings (search_limit * rate * 10).
        sample_size = max(1, int(self.config.server.memory.reflection.search_limit * rate * 10))

        # Use random() for sampling. For larger tables, TABLESAMPLE would be better,
        # but for typical user vaults, random() is sufficient and more portable.
        query = (
            select(MemoryUnit)
            .where(
                col(MemoryUnit.is_deprioritized).is_(False),
                (col(MemoryUnit.vault_id) == vault_id)
                | (col(MemoryUnit.vault_id) == GLOBAL_VAULT_ID),
            )
            .order_by(func.random())
            .limit(sample_size)
            .options(defer(MemoryUnit.embedding))  # type: ignore
        )

        result = await session.exec(query)
        return list(result.all())

    async def _phase_3_validate(
        self,
        candidates_with_evidence: list[tuple[CandidateObservation, list[MemoryUnit]]],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> list[ValidatedObservation]:
        """Phase 3: Validate candidates against evidence."""
        if not candidates_with_evidence:
            return []

        all_memory_ids = []
        for _, mems in candidates_with_evidence:
            for m in mems:
                all_memory_ids.append(m.id)

        uuid_to_int, int_to_uuid = create_citation_map(all_memory_ids)

        candidate_observations = []
        for cand, mems in candidates_with_evidence:
            index_map = {m.id: uuid_to_int.get(str(m.id), -1) for m in mems}
            context_objs = build_memory_context(mems, index_map=index_map)

            candidate_observations.append(
                UnvalidatedCandidateObservation(content=cand.content, context=context_objs)
            )

        validate_predictor = dspy.Predict(ValidatePhaseSignature)

        if self.lm is None:
            raise RuntimeError('LM must be initialized')
        result = await run_dspy_operation(
            lm=self.lm,
            predictor=validate_predictor,
            input_kwargs={'candidates': candidate_observations},
            operation_name='reflection.validate',
        )

        if result is None:
            raise RuntimeError('Phase 3 Validate failed (LLM returned None).')

        if not result.validated_observations:
            logger.warning('Phase 3 Validate returned no observations.')
            return []

        for val_obs in result.validated_observations:
            for ev in val_obs.evidence:
                try:
                    int_id = int(ev.memory_id)
                    if int_id in int_to_uuid:
                        ev.memory_id = int_to_uuid[int_id]
                    else:
                        logger.warning(f'Phase 3: No UUID mapping for evidence ID: {int_id}')
                except (ValueError, TypeError):
                    logger.warning(f'Phase 3: Invalid evidence memory_id format: {ev.memory_id}')

        return result.validated_observations

    async def _phase_4_compare(
        self,
        existing: list[Observation],
        new_obs: list[ValidatedObservation],
        vault_id: UUID = GLOBAL_VAULT_ID,
        entity_name: str = '',
    ) -> tuple[list[Observation], str]:
        """Phase 4: Merge validated observations with existing. Returns (observations, summary)."""
        if not new_obs:
            return existing, ''

        # 1. Collect all unique evidence to build a shared context
        all_uuids = set()

        # From existing
        for o in existing:
            if o.evidence:
                for ev in o.evidence:
                    all_uuids.add(str(ev.memory_id))

        # From new
        for o in new_obs:
            if o.evidence:
                for ev in o.evidence:
                    # These might be UUID strings (restored in Phase 3) or ints if resolution failed
                    all_uuids.add(str(ev.memory_id))

        # Filter invalid UUIDs
        valid_uuids = []
        evidence_data_map = {}  # uuid -> {quote, timestamp}

        # Helper to hydrate evidence map
        def hydrate(obs_list):
            for o in obs_list:
                if o.evidence:
                    for ev in o.evidence:
                        # memory_id can be an int (index) or a UUID string
                        uid = str(ev.memory_id)
                        try:
                            # If it's a UUID string, track it
                            UUID(uid)
                            if uid not in evidence_data_map:
                                evidence_data_map[uid] = {
                                    'quote': ev.quote or 'Content unavailable',
                                    'timestamp': ev.timestamp,
                                }
                        except ValueError:
                            # If it's an int/index, it should already be in existing or new_obs.
                            # However, for new_obs specifically, we might have indices from Phase 3.
                            # BUT Phase 4 expects to build a GLOBAL index map.
                            # So we actually need the original UUIDs for all evidence.
                            pass

        hydrate(existing)
        hydrate(new_obs)

        # Create map
        valid_uuids = sorted(evidence_data_map.keys())
        uuid_to_int, int_to_uuid = create_citation_map(valid_uuids)

        # 2. Build Structured Contexts
        evidence_context = []
        for idx in range(len(valid_uuids)):
            uid = int_to_uuid[idx]
            data = evidence_data_map[uid]
            evidence_context.append(
                ReflectEvidenceContext(
                    index_id=idx,
                    quote=data['quote'],
                    occurred=str(data['timestamp']),
                )
            )

        def map_indices(obs_list) -> list[ReflectComparisonObservation]:
            result_list = []
            for i, o in enumerate(obs_list):
                indices = []
                if o.evidence:
                    for ev in o.evidence:
                        idx = uuid_to_int.get(str(ev.memory_id))
                        if idx is not None:
                            indices.append(idx)

                result_list.append(
                    ReflectComparisonObservation(
                        index_id=i, title=o.title, content=o.content, evidence_indices=indices
                    )
                )
            return result_list

        existing_ctx = map_indices(existing)
        new_ctx = map_indices(new_obs)

        # 3. Call LLM
        compare_predictor = dspy.Predict(ComparePhaseSignature)

        if self.lm is None:
            raise RuntimeError('LM must be initialized')
        result = await run_dspy_operation(
            lm=self.lm,
            predictor=compare_predictor,
            input_kwargs={
                'entity_name': entity_name,
                'evidence_context': evidence_context,
                'existing_context': existing_ctx,
                'new_context': new_ctx,
            },
            operation_name='reflection.compare',
        )

        if not result or not result.result or not result.result.observations:
            raise RuntimeError('Phase 4 Compare failed (LLM output error).')

        # 4. Reconstruct Observations.
        # Stable IDs come from result.result.provenance: lowest-existing-index wins
        # on 'merged'/'kept'; 'added' or malformed → fresh uuid4(). The default_factory
        # on Observation.id handles the fresh-uuid4 path.
        provenance = getattr(result.result, 'provenance', None) or []
        output_count = len(result.result.observations)
        if not provenance:
            # LLM omitted the entire list — every output observation gets a
            # fresh uuid4, losing all existing stable IDs for this entity.
            # One increment per occurrence keeps the counter unitary; the
            # ``output_count`` breadth is observable separately via the
            # phase 4 output_count histogram if/when it lands.
            PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='empty').inc()
        elif len(provenance) != output_count:
            PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='length_mismatch').inc()
            provenance = []  # ignore the entire list; per-output fresh uuid4

        # Build lookup by output_index (LLM may emit out-of-order entries).
        # Track existing UUIDs already consumed by a 'merged'/'kept' output so
        # the same UUID can't be assigned to two output observations (which
        # would leave one un-refreshable via observation_id).
        provenance_by_index: dict[int, Any] = {}
        for prov in provenance:
            out_idx = getattr(prov, 'output_index', None)
            if isinstance(out_idx, int) and 0 <= out_idx < output_count:
                provenance_by_index[out_idx] = prov
            else:
                # 'output_index_oob' distinguishes this from
                # 'existing_index_oob' in `_resolve_provenance_uuid` —
                # different prompt-drift signals deserve separate labels.
                PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='output_index_oob').inc()

        final_list = []
        seen_uuids: set[UUID] = set()
        for i, val_obs in enumerate(result.result.observations):
            evidence_models = []
            for ev in val_obs.evidence:
                try:
                    # NewEvidenceItem from LLM likely has index in memory_id
                    idx_val = int(ev.memory_id)
                    if idx_val in int_to_uuid:
                        original_uuid = UUID(int_to_uuid[idx_val])
                        evidence_models.append(
                            EvidenceItem(
                                memory_id=original_uuid,
                                quote=ev.quote,
                                relevance=1.0,
                                explanation=ev.relevance_explanation,
                                timestamp=parse_timestamp(ev.timestamp),
                            )
                        )
                    else:
                        logger.warning(f'Phase 4: Evidence index out of bounds: {idx_val}')
                except (ValueError, TypeError):
                    logger.warning(f'Phase 4: Invalid evidence index format: {ev.memory_id}')

            # Compute Trend based on evidence timestamps
            trend = compute_trend(evidence_models)

            prov_entry = provenance_by_index.get(i)
            obs_id = _resolve_provenance_uuid(prov_entry, existing)
            # Collision: two outputs resolved to the same existing UUID (e.g.
            # the LLM reused 'merged' indices across outputs). Fall back to
            # uuid4 for the second occurrence; the first keeps the stable id.
            collided = obs_id in seen_uuids
            if collided:
                PHASE4_PROVENANCE_MALFORMED_TOTAL.labels(reason='uuid_collision').inc()
                obs_id = uuid4()
            seen_uuids.add(obs_id)
            # Predecessor counting: only count when we kept the lowest-idx UUID
            # (no collision). On collision the merge output is downgraded to
            # 'added' (fresh uuid4); the "predecessors dropped" framing no
            # longer applies — counting it would inflate the metric.
            if not collided and prov_entry is not None:
                idxs = getattr(prov_entry, 'merged_from_existing_indices', None) or []
                # `_resolve_provenance_uuid` only returns a stable existing UUID
                # when status in {'kept', 'merged'} and all indices were valid;
                # in that case len(idxs) > 1 means N-1 UUIDs were discarded.
                if len(idxs) > 1 and all(0 <= idx < len(existing) for idx in idxs):
                    REFRESH_OBSERVATION_MERGED_PREDECESSOR_TOTAL.inc(len(idxs) - 1)

            final_list.append(
                Observation(
                    id=obs_id,
                    title=val_obs.title,
                    content=val_obs.content,
                    evidence=evidence_models,
                    trend=trend,
                )
            )

        raw_summary = getattr(result.result, 'entity_summary', '')
        entity_summary = raw_summary if isinstance(raw_summary, str) else ''
        return final_list, entity_summary
