"""
The Hindsight "Reflect" Engine.

This module orchestrates the reflection loop (Phases 0-6) to update Mental Models
based on new evidence and memories.
"""

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID
from collections import defaultdict

import dspy
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer
from sqlalchemy.orm.attributes import flag_modified

from memex_core.config import MemexConfig, GLOBAL_VAULT_ID
from memex_core.llm import run_dspy_operation
from memex_core.tracing import trace_span
from memex_core.memory.sql_models import Entity, MemoryUnit, UnitEntity, ContentStatus
from memex_core.memory.sql_models import MentalModel, Observation, EvidenceItem
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
)
from memex_core.memory.reflect.utils import (
    build_memory_context,
    create_citation_map,
    parse_timestamp,
)
from memex_core.memory.reflect.trends import compute_trend
from memex_core.memory.models.protocols import EmbeddingsModel
from memex_core.memory.formatting import format_for_embedding

logger = logging.getLogger('memex.core.memory.reflect.reflection')


def get_reflection_engine(
    session: AsyncSession,
    config: MemexConfig,
    embedder: EmbeddingsModel,
) -> 'ReflectionEngine':
    """
    Factory method to create a ReflectionEngine with dependencies.
    """
    return ReflectionEngine(
        session=session,
        config=config,
        embedder=embedder,
    )


class ReflectionEngine:
    """
    The ReflectionEngine (formerly ReflectAgent) orchestrates the periodic "Reflection" phase
    of the Hindsight architecture.
    """

    def __init__(
        self,
        session: AsyncSession,
        config: MemexConfig,
        embedder: EmbeddingsModel,
    ):
        self.session = session
        self.config = config or MemexConfig()
        self.embedder = embedder

        # Ideally, we should receive the LM here, but we'll try to use dspy.settings.lm if not provided
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
                    )
        except (ValueError, RuntimeError, OSError, KeyError, AttributeError) as e:
            logger.warning(
                'Could not initialize LM: %s. Reflection might fail if LM is not set.', e
            )
            self.lm = None

    async def reflect_batch(self, requests: list[ReflectionRequest]) -> list[MentalModel]:
        """
        Run reflection for multiple entities in parallel.
        Groups requests by vault_id to ensure correct scoping.
        """
        if not requests:
            return []

        from collections import defaultdict

        # 1. Group by Vault ID to optimize DB fetching
        vault_groups = defaultdict(list)
        for r in requests:
            vault_groups[r.vault_id].append(r)

        logger.info(
            f'Starting batch reflection for {len(requests)} entities across {len(vault_groups)} vaults'
        )

        all_success_models = []
        db_lock = asyncio.Lock()
        concurrency = self.config.server.memory.reflection.max_concurrency
        sem = asyncio.Semaphore(concurrency)

        for vault_id, v_requests in vault_groups.items():
            entity_ids = [r.entity_id for r in v_requests]

            # 1.1 Batch Load Data for this Vault (Serial DB Access)
            models_map = await self._batch_get_or_create_models(entity_ids, vault_id=vault_id)
            entities_map = await self._batch_get_entities(entity_ids)
            memories_map = await self._batch_fetch_recent_memories(entity_ids, vault_id=vault_id)

            # 1.2 Concurrent Processing for this Vault
            results = await asyncio.gather(
                *[
                    self._process_entity_reflection(
                        req,
                        models_map,
                        entities_map,
                        memories_map,
                        sem,
                        db_lock,
                    )
                    for req in v_requests
                ]
            )
            all_success_models.extend([m for m in results if m is not None])

        if not all_success_models:
            return []

        # 2. Optimistic Batch Persistence
        try:
            await self.session.commit()
            logger.info(f'Successfully committed batch of {len(all_success_models)} mental models.')
        except (SQLAlchemyError, OSError, RuntimeError) as e:
            logger.error(f'Batch commit failed: {e}. Attempting rescue mode...')
            await self._batch_save_rescue(all_success_models)

        return all_success_models

    async def _process_entity_reflection(
        self,
        req: ReflectionRequest,
        models_map: dict[UUID, MentalModel],
        entities_map: dict[UUID, Entity],
        memories_map: dict[UUID, list[MemoryUnit]],
        sem: asyncio.Semaphore,
        db_lock: asyncio.Lock,
    ) -> MentalModel | None:
        """Process a single entity reflection with semaphore control."""
        eid = req.entity_id
        async with sem:
            try:
                entity = entities_map.get(eid)
                return await self._reflect_entity_internal(
                    entity_id=eid,
                    mental_model=models_map[eid],
                    entity=entity,
                    recent_memories=memories_map.get(eid, []),
                    db_lock=db_lock,
                    vault_id=req.vault_id,
                )
            except Exception as e:
                logger.error(f'Reflection failed for entity {eid}: {e}', exc_info=True)
                return None

    async def _batch_save_rescue(self, models: list[MentalModel]) -> None:
        """Attempt to save models one by one if batch commit fails."""
        await self.session.rollback()

        saved_count = 0
        for model in models:
            try:
                self.session.add(model)
                flag_modified(model, 'observations')
                flag_modified(model, 'entity_metadata')
                await self.session.commit()
                saved_count += 1
            except (SQLAlchemyError, OSError, RuntimeError) as inner_e:
                logger.error(f'Rescue save failed for model {model.id}: {inner_e}')
                await self.session.rollback()
        logger.info(f'Rescue mode saved {saved_count}/{len(models)} models.')

    async def _reflect_entity_internal(
        self,
        entity_id: UUID,
        mental_model: MentalModel,
        entity: Entity | None,
        recent_memories: list[MemoryUnit],
        db_lock: asyncio.Lock,
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> MentalModel:
        """Internal logic for single entity reflection, decoupled from DB fetching logic."""
        entity_name = entity.canonical_name if entity else 'Unknown'
        entity_type = entity.entity_type if entity else None

        # 0. Acquire Advisory Lock for this Entity to prevent concurrent reflection
        # Use transaction-level lock (automatically released at end of transaction/session)
        # Using hashtext for consistent 64-bit int generation from UUID string
        # Note: We rely on the caller's session transaction context.
        lock_query = select(func.pg_try_advisory_xact_lock(func.hashtext(f'reflect:{entity_id}')))
        is_locked = (await self.session.exec(lock_query)).one()

        if not is_locked:
            logger.warning(
                f'Skipping reflection for entity {entity_id}: Could not acquire advisory lock (already running).'
            )
            return mental_model

        with trace_span(
            'memex.reflection',
            'reflection',
            {
                'reflection.entity_id': str(entity_id),
                'reflection.entity_name': entity_name,
                'reflection.vault_id': str(vault_id),
            },
        ):
            # Phase 0: Update Existing
            updated_observations = await self._phase_0_update(
                mental_model, entity_name, recent_memories, vault_id=vault_id
            )

            if not recent_memories:
                mental_model.observations = [
                    obs.model_dump(mode='json') for obs in updated_observations
                ]
                return mental_model

            # Phase 1: Seed (LLM)
            candidates = await self._phase_1_seed(
                recent_memories, entity_name, updated_observations, vault_id=vault_id
            )

            # Phase 2: Hunt (Vector Search)
            candidates_with_evidence = await self._phase_2_hunt(
                candidates, db_lock, vault_id=vault_id
            )

            # Phase 3: Validate (LLM)
            validated = await self._phase_3_validate(candidates_with_evidence, vault_id=vault_id)

            # Phase 4: Compare (LLM)
            final_obs, entity_summary = await self._phase_4_compare(
                updated_observations, validated, vault_id=vault_id, entity_name=entity_name
            )

            # Phase 5: Finalize Model
            await self._phase_5_finalize(
                mental_model,
                final_obs,
                db_lock,
                entity_summary=entity_summary,
                entity_type=entity_type,
            )

            # Phase 6: Enrich (Memory Evolution)
            if self.config.server.memory.reflection.enrichment_enabled:
                await self._phase_6_enrich(
                    entity_name=entity_name,
                    entity_summary=entity_summary,
                    final_obs=final_obs,
                    recent_memories=recent_memories,
                    db_lock=db_lock,
                    vault_id=vault_id,
                )

            return mental_model

    async def _phase_5_finalize(
        self,
        mental_model: MentalModel,
        final_obs: list[Observation],
        db_lock: asyncio.Lock,
        entity_summary: str = '',
        entity_type: str | None = None,
    ) -> None:
        """
        Phase 5: Prepare Model (CPU/GPU).
        Updates observations, version, embedding, and entity metadata.
        """
        mental_model.observations = [obs.model_dump(mode='json') for obs in final_obs]
        mental_model.version += 1
        mental_model.last_refreshed = datetime.now(timezone.utc)

        mental_model.entity_metadata = {
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
        mental_model.embedding = embedding_list[0]

        async with db_lock:
            self.session.add(mental_model)
            flag_modified(mental_model, 'observations')
            flag_modified(mental_model, 'entity_metadata')

    async def _phase_6_enrich(
        self,
        entity_name: str,
        entity_summary: str,
        final_obs: list[Observation],
        recent_memories: list[MemoryUnit],
        db_lock: asyncio.Lock,
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> None:
        """
        Phase 6: Enrich (Memory Evolution).
        Pushes enriched tags from the mental model back into contributing memory units,
        making them discoverable for concepts identified during reflection.
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

        # 2. Build unit map from recent_memories, load any missing from DB
        unit_map: dict[UUID, MemoryUnit] = {m.id: m for m in recent_memories}
        missing_ids = set(evidence_ids.keys()) - set(unit_map.keys())

        if missing_ids:
            async with db_lock:
                stmt = select(MemoryUnit).where(col(MemoryUnit.id).in_(list(missing_ids)))
                result = await self.session.exec(stmt)
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

        # 5. Call LLM
        enrich_predictor = dspy.Predict(EnrichmentSignature)

        assert self.lm is not None, 'LM must be initialized for Phase 6'
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

        # 6. Apply enrichments under db_lock
        now_iso = datetime.now(timezone.utc).isoformat()
        enriched_count = 0

        async with db_lock:
            for enrichment in result.enrichments:
                idx = enrichment.memory_index
                if idx < 0 or idx >= len(target_units):
                    logger.warning(f'Phase 6: Invalid memory_index {idx}, skipping.')
                    continue

                unit = target_units[idx]
                if unit.unit_metadata is None:
                    unit.unit_metadata = {}

                # Set-union: accumulate tags across reflection cycles
                existing_tags = set(unit.unit_metadata.get('enriched_tags', []))
                existing_kw = set(unit.unit_metadata.get('enriched_keywords', []))

                new_tags = existing_tags | {t.lower().strip() for t in enrichment.enriched_tags}
                new_kw = existing_kw | {k.lower().strip() for k in enrichment.enriched_keywords}

                unit.unit_metadata['enriched_tags'] = sorted(new_tags)
                unit.unit_metadata['enriched_keywords'] = sorted(new_kw)
                unit.unit_metadata['enriched_at'] = now_iso
                unit.unit_metadata['enriched_by_entity'] = entity_name

                flag_modified(unit, 'unit_metadata')
                enriched_count += 1

        logger.info(f'Phase 6: Enriched {enriched_count} memory units for entity "{entity_name}".')

    async def _batch_get_or_create_models(
        self, entity_ids: list[UUID], vault_id: UUID = GLOBAL_VAULT_ID
    ) -> dict[UUID, MentalModel]:
        """Batch fetch or create mental models."""
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
        limit_per_entity: int = 20,
    ) -> dict[UUID, list[MemoryUnit]]:
        """
        Fetch recent memories for multiple entities in one go using Window Function.
        Vault scoping follows "Fall-through" logic: (vault_id == active OR vault_id == Global).
        """

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
            .where(subq.c.rn <= limit_per_entity)
            .options(defer(MemoryUnit.embedding))  # type: ignore
        )

        results = (await self.session.exec(query)).all()

        memories_map = defaultdict(list)
        for unit, eid in results:
            memories_map[eid].append(unit)

        return memories_map

    async def reflect_on_entity(self, request: ReflectionRequest) -> MentalModel:
        """Legacy wrapper for single entity reflection."""
        results = await self.reflect_batch([request])
        if not results:
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
    ) -> list[Observation]:
        """
        Phase 0: Check if existing observations have new supporting/contradicting evidence.
        Also prunes stale evidence referencing deleted memory units (liveness check).
        """
        current_observations = [Observation(**obs) for obs in model.observations]
        if not current_observations:
            return current_observations

        # Liveness check: prune evidence citing deleted memory units
        all_evidence_ids: set[UUID] = set()
        for obs in current_observations:
            for ev in obs.evidence:
                all_evidence_ids.add(ev.memory_id)

        if all_evidence_ids:
            live_stmt = select(MemoryUnit.id).where(
                col(MemoryUnit.id).in_(list(all_evidence_ids)),
                (col(MemoryUnit.vault_id) == vault_id)
                | (col(MemoryUnit.vault_id) == GLOBAL_VAULT_ID),
            )
            live_result = await self.session.exec(live_stmt)
            live_ids = set(live_result.all())
            dead_ids = all_evidence_ids - live_ids

            if dead_ids:
                pruned = False
                pruned_to_empty: set[UUID] = set()
                for obs in current_observations:
                    original_len = len(obs.evidence)
                    obs.evidence = [ev for ev in obs.evidence if ev.memory_id not in dead_ids]
                    if len(obs.evidence) < original_len:
                        pruned = True
                        if not obs.evidence:
                            pruned_to_empty.add(obs.id)

                # Only drop observations that were pruned to empty, not naturally empty ones
                if pruned_to_empty:
                    current_observations = [
                        obs for obs in current_observations if obs.id not in pruned_to_empty
                    ]

                if pruned:
                    model.observations = [
                        obs.model_dump(mode='json') for obs in current_observations
                    ]
                    flag_modified(model, 'observations')

        if not current_observations or not memories:
            return current_observations

        memory_map = {i: m for i, m in enumerate(memories)}

        memory_context = build_memory_context(memories)

        obs_context = [
            ReflectObservationContext(index_id=i, title=o.title, content=o.content)
            for i, o in enumerate(current_observations)
        ]

        update_predictor = dspy.Predict(UpdateExistingSignature)

        assert self.lm is not None, 'LM must be initialized'
        result = await run_dspy_operation(
            lm=self.lm,
            predictor=update_predictor,
            input_kwargs={'recent_memories': memory_context, 'existing_observations': obs_context},
            operation_name='reflection.update',
        )

        if not result or not result.updates:
            return current_observations

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

                if update.has_contradiction:
                    note = update.contradiction_note or 'New evidence contradicts this observation.'
                    obs.content += f' [CONTRADICTION: {note}]'

        return current_observations

    async def _phase_1_seed(
        self,
        memories: list[MemoryUnit],
        topic: str,
        existing_obs: list[Observation],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> list[CandidateObservation]:
        """
        Phase 1: Generate candidates from recent memories.
        """
        if not memories:
            return []

        memory_context = build_memory_context(memories)

        obs_context = [
            ReflectObservationContext(index_id=i, title=o.title, content=o.content)
            for i, o in enumerate(existing_obs)
        ]

        seed_predictor = dspy.Predict(SeedPhaseSignature)

        assert self.lm is not None, 'LM must be initialized'
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
        db_lock: asyncio.Lock,
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> list[tuple[CandidateObservation, list[MemoryUnit]]]:
        """
        Phase 2: Retrieve evidence for candidates.
        """
        from memex_core.memory.extraction import storage

        if not candidates:
            return []

        texts = [c.content for c in candidates]
        embeddings = await self._async_encode(texts)
        results: list[tuple[CandidateObservation, list[MemoryUnit]]] = []

        # 2b. Tail Sampling: Sample random memories from the vault
        tail_memories = await self._sample_tail_memories(vault_id=vault_id)

        for i, cand in enumerate(candidates):
            embedding = embeddings[i]

            async with db_lock:
                similar_items = await storage.find_similar_facts(
                    self.session,
                    embedding,
                    limit=self.config.server.memory.reflection.search_limit,
                    threshold=self.config.server.memory.reflection.similarity_threshold,
                    vault_ids=[vault_id],
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
                units_result = await self.session.exec(unit_stmt)
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

    async def _sample_tail_memories(self, vault_id: UUID) -> list[MemoryUnit]:
        """
        Sample random memories from the vault (Tail Sampling).
        Provides a small percentage of 'non-similar' memories to avoid echo chambers.
        """
        rate = self.config.server.memory.reflection.tail_sampling_rate
        if rate <= 0:
            return []

        # Calculate sample size. We want a small bonus proportional to the search limit.
        # e.g. if rate=0.05 and search_limit=10, we sample at least 1 memory.
        # Here we use a heuristic: sample_size = max(1, round(search_limit * rate * 5))
        # This yields ~2-3 memories for default settings (10 * 0.05 * 5 = 2.5).
        sample_size = max(1, int(self.config.server.memory.reflection.search_limit * rate * 10))

        # Use random() for sampling. For larger tables, TABLESAMPLE would be better,
        # but for typical user vaults, random() is sufficient and more portable.
        query = (
            select(MemoryUnit)
            .where(
                (col(MemoryUnit.vault_id) == vault_id)
                | (col(MemoryUnit.vault_id) == GLOBAL_VAULT_ID)
            )
            .order_by(func.random())
            .limit(sample_size)
            .options(defer(MemoryUnit.embedding))  # type: ignore
        )

        result = await self.session.exec(query)
        return list(result.all())

    async def _phase_3_validate(
        self,
        candidates_with_evidence: list[tuple[CandidateObservation, list[MemoryUnit]]],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> list[ValidatedObservation]:
        """
        Phase 3: Validate candidates against evidence.
        """
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

        assert self.lm is not None, 'LM must be initialized'
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
        """
        Phase 4: Merge new validated observations with existing ones.
        Returns (final_observations, entity_summary).
        """
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

        assert self.lm is not None, 'LM must be initialized'
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

        # 4. Reconstruct Observations
        final_list = []
        for val_obs in result.result.observations:
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

            final_list.append(
                Observation(
                    title=val_obs.title,
                    content=val_obs.content,
                    evidence=evidence_models,
                    trend=trend,
                )
            )

        raw_summary = getattr(result.result, 'entity_summary', '')
        entity_summary = raw_summary if isinstance(raw_summary, str) else ''
        return final_list, entity_summary
