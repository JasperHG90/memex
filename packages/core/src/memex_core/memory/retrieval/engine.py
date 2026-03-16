import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID
import asyncio
from datetime import datetime, timezone
from typing import Any, Sequence
import math

import numpy as np
import tiktoken
from cachetools import TTLCache
from sqlalchemy import func, literal, union_all
from sqlalchemy.orm import defer, selectinload
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import RetrievalConfig, ReflectionConfig
from memex_core.memory.models.embedding import FastEmbedder, get_embedding_model
from memex_core.memory.models.reranking import FastReranker, get_reranking_model
from memex_core.memory.models.ner import FastNERModel, get_ner_model
from memex_core.memory.retrieval.strategies import (
    KeywordStrategy,
    RetrievalStrategy,
    SemanticStrategy,
    TemporalStrategy,
    MentalModelStrategy,
    get_graph_strategy,
)
from memex_core.memory.retrieval.expansion import QueryExpander
from memex_core.memory.retrieval.temporal_extraction import extract_temporal_constraint
from memex_core.memory.sql_models import MemoryUnit, MentalModel, UnitEntity, ContentStatus
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_common.types import FactTypes
from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.formatting import format_for_reranking

logger = logging.getLogger('memex.core.memory.retrieval.engine')


def derive_note_status(units: list[MemoryUnit], superseded_threshold: float = 0.3) -> str:
    """Derive note-level status from unit confidences."""
    if not units:
        return 'active'
    low_confidence = sum(1 for u in units if getattr(u, 'confidence', 1.0) < superseded_threshold)
    ratio = low_confidence / len(units)
    if ratio > 0.5:
        return 'superseded'
    elif low_confidence > 0:
        return 'partially_superseded'
    return 'active'


# RRF Constant
K_RRF = 60
CANDIDATE_POOL_SIZE = 60


@dataclass
class StrategyContribution:
    """Tracks a single strategy's contribution to a result."""

    strategy_name: str
    rank: int  # 1-based rank within strategy
    rrf_score: float
    raw_score: float | None = None
    timing_ms: float | None = None


@dataclass
class DebugContext:
    """Collects debug info across the retrieval pipeline."""

    strategy_timings: dict[str, float] = field(default_factory=dict)
    per_result: dict[UUID, list[StrategyContribution]] = field(
        default_factory=lambda: defaultdict(list)
    )


async def get_retrieval_engine(
    embedder: FastEmbedder | None = None,
    reranker: FastReranker | None = None,
    ner_model: FastNERModel | None = None,
    reflection_config: ReflectionConfig | None = None,
    retrieval_config: RetrievalConfig | None = None,
    lm: Any | None = None,
) -> 'RetrievalEngine':
    """
    Factory method to create a RetrievalEngine with dependencies.
    """
    if embedder is None:
        embedder = await get_embedding_model()
    if reranker is None:
        try:
            reranker = await get_reranking_model()
        except (ImportError, ValueError, RuntimeError, OSError) as e:
            logger.debug('Reranking model unavailable, skipping: %s', e)
            reranker = None
    if ner_model is None:
        try:
            ner_model = await get_ner_model()
        except (ImportError, ValueError, RuntimeError, OSError) as e:
            logger.debug('NER model unavailable, skipping: %s', e)
            ner_model = None

    return RetrievalEngine(
        embedder=embedder,
        reranker=reranker,
        ner_model=ner_model,
        reflection_config=reflection_config,
        retrieval_config=retrieval_config,
        lm=lm,
    )


class RetrievalEngine:
    """
    Orchestrates memory retrieval using the 4-channel Hindsight architecture (TEMPR Recall).
    Fuses results purely in SQL using CTEs and Reciprocal Rank Fusion.
    """

    def __init__(
        self,
        embedder: FastEmbedder,
        reranker: FastReranker | None = None,
        ner_model: FastNERModel | None = None,
        reflection_config: ReflectionConfig | None = None,
        retrieval_config: RetrievalConfig | None = None,
        lm: Any | None = None,
        session_factory: Any | None = None,
    ):
        self.embedder = embedder
        self.reranker = reranker
        self.ner_model = ner_model
        self.retrieval_config = retrieval_config or RetrievalConfig()
        self.lm = lm
        self.expander = QueryExpander(lm=self.lm) if self.lm else None
        self._session_factory = session_factory

        # Source RRF constants from config
        self.k_rrf = self.retrieval_config.rrf_k
        self.candidate_pool_size = self.retrieval_config.candidate_pool_size

        # Query embedding cache: avoids re-encoding recently seen queries
        self._embedding_cache: TTLCache[str, np.ndarray] = TTLCache(maxsize=256, ttl=300)
        self._embedding_cache_lock = asyncio.Lock()

        from memex_core.memory.reflect.queue_service import ReflectionQueueService

        self.queue_service = (
            ReflectionQueueService(config=reflection_config) if reflection_config else None
        )
        self.strategies: dict[str, tuple[RetrievalStrategy, bool]] = {
            'semantic': (SemanticStrategy(), False),  # False = ASC (Distance)
            'keyword': (KeywordStrategy(), True),  # True = DESC (Score)
            'graph': (
                get_graph_strategy(
                    type=self.retrieval_config.graph_retriever_type,
                    ner_model=self.ner_model,
                    similarity_threshold=self.retrieval_config.similarity_threshold,
                    temporal_decay_days=self.retrieval_config.temporal_decay_days,
                    temporal_decay_base=self.retrieval_config.temporal_decay_base,
                ),
                True,
            ),  # True = DESC
            'temporal': (TemporalStrategy(), True),  # True = DESC
        }
        self.mm_strategy = MentalModelStrategy()

    async def _get_embeddings_cached(self, queries: list[str]) -> np.ndarray:
        """Return embeddings for *queries*, serving cache hits and batch-encoding misses."""
        results: list[np.ndarray] = []
        misses: list[tuple[int, str]] = []  # (index, query_text)

        async with self._embedding_cache_lock:
            for i, q in enumerate(queries):
                key = hashlib.sha256(q.encode()).hexdigest()
                cached = self._embedding_cache.get(key)
                if cached is not None:
                    results.append(cached)
                else:
                    results.append(np.empty(0))  # placeholder
                    misses.append((i, q))

        if misses:
            miss_texts = [q for _, q in misses]
            encoded = await asyncio.to_thread(self.embedder.encode, miss_texts)
            async with self._embedding_cache_lock:
                for (idx, q), emb in zip(misses, encoded):
                    key = hashlib.sha256(q.encode()).hexdigest()
                    self._embedding_cache[key] = emb
                    results[idx] = emb

        return np.array(results)

    async def retrieve(
        self,
        session: AsyncSession,
        request: RetrievalRequest,
    ) -> tuple[list[MemoryUnit], dict[str, Any] | None]:
        """
        Retrieve memories and synthesized observations using In-DB RRF.
        If a reranker is available, fetches a larger pool and re-ranks them.
        """
        # 1. Query Expansion (Multi-Query)
        queries = [request.query]
        query_weights = [2.0]  # Original query is weighted higher

        primary_vault_id = request.vault_ids[0] if request.vault_ids else GLOBAL_VAULT_ID

        if request.expand_query and self.expander:
            variations, _ = await self.expander.expand(
                request.query, session=session, vault_id=primary_vault_id
            )
            for var in variations:
                queries.append(var)
                query_weights.append(1.0)

        # 2. Get Embeddings for all queries (with per-query caching)
        all_embeddings = await self._get_embeddings_cached(queries)

        # 3. Determine budget and limit
        token_budget = request.token_budget
        if token_budget is None and self.retrieval_config:
            token_budget = self.retrieval_config.token_budget

        effective_limit = request.limit
        if token_budget is not None and effective_limit < 50:
            effective_limit = 50

        use_reranker = self.reranker is not None and request.rerank
        candidate_depth = max(effective_limit * 3, 50) if use_reranker else effective_limit

        # 3b. NLP Temporal Extraction (upstream of RRF)
        # Only extract if no explicit date filters were provided and feature is enabled.
        filters = dict(request.filters) if request.filters else {}
        if (
            self.retrieval_config.temporal_extraction_enabled
            and 'start_date' not in filters
            and 'end_date' not in filters
        ):
            temporal_range = extract_temporal_constraint(request.query)
            if temporal_range is not None:
                filters['start_date'] = temporal_range[0]
                filters['end_date'] = temporal_range[1]
                logger.debug(
                    'Temporal extraction: %s -> %s to %s',
                    request.query,
                    temporal_range[0],
                    temporal_range[1],
                )

        # 4. Perform Retrieval (Fused across all queries)
        if request.vault_ids:
            filters['vault_ids'] = request.vault_ids

        # Explicitly pass include_stale flag to strategies
        filters['include_stale'] = request.include_stale

        # Pre-compute NER entities off the event loop so graph strategies don't block
        if self.ner_model is not None:
            try:
                filters['_ner_entities'] = await asyncio.to_thread(
                    self.ner_model.predict, request.query
                )
            except (ValueError, RuntimeError, OSError) as e:
                logger.warning('NER pre-extraction failed: %s', e)

        # Thread temporal filters for strategy-level date filtering
        if request.after:
            filters['start_date'] = request.after
        if request.before:
            filters['end_date'] = request.before

        debug_ctx: DebugContext | None = DebugContext() if request.debug else None

        use_partitioned = self.retrieval_config.fact_type_partitioned_rrf

        all_ranked_items = []
        for q, q_emb, q_weight in zip(queries, all_embeddings, query_weights):
            if use_partitioned:
                items = await self._perform_partitioned_rrf(
                    session,
                    q,
                    q_emb.tolist(),
                    candidate_depth,
                    filters,
                    strategies=request.strategies,
                    strategy_weights=request.strategy_weights,
                    debug_ctx=debug_ctx,
                )
            else:
                items = await self._perform_rrf_retrieval(
                    session,
                    q,
                    q_emb.tolist(),
                    candidate_depth,
                    filters,
                    strategies=request.strategies,
                    strategy_weights=request.strategy_weights,
                    debug_ctx=debug_ctx,
                )
            # Weighted candidates for multi-query fusion
            all_ranked_items.append((items, q_weight))

        # Free embedding arrays — can be ~100KB+ and no longer needed
        del all_embeddings

        if not all_ranked_items:
            return ([], None)

        # 5. Multi-Query RRF Fusion (Final Blend)
        fused_items = self._fuse_multi_query_results(all_ranked_items, candidate_depth)

        if not fused_items:
            return ([], None)

        # 6. Hydrate Objects
        final_results = await self._hydrate_results(session, fused_items)

        # 6b. Filter superseded units
        if not request.include_superseded:
            threshold = self.retrieval_config.superseded_threshold
            final_results = [u for u in final_results if getattr(u, 'confidence', 1.0) >= threshold]

        # 7. Rerank
        if use_reranker:
            # Rerank against original query
            final_results = await self._rerank_results(
                request.query, final_results, min_score=request.min_score
            )

        # 8. Position-Aware Blending
        if request.fusion_strategy == 'position_aware' and use_reranker:
            final_results = self._apply_position_aware_blending(final_results)

        # 9. Attach Citations
        final_results = self._attach_citations(final_results)

        # 9b. MMR diversity filtering
        mmr_lambda = request.mmr_lambda
        if mmr_lambda is None and self.retrieval_config:
            mmr_lambda = self.retrieval_config.mmr_lambda
        if mmr_lambda is not None and len(final_results) > 1:
            # Split out virtual observations (no real embeddings) — they would get
            # an unfair diversity advantage because cosine returns 0.0 for them.
            real_units = []
            virtual_positions: list[tuple[int, MemoryUnit]] = []
            for idx, u in enumerate(final_results):
                if u.unit_metadata.get('virtual'):
                    virtual_positions.append((idx, u))
                else:
                    real_units.append(u)

            if real_units and len(real_units) > 1:
                unit_ids = [u.id for u in real_units]
                cosine_matrix = await self._compute_pairwise_cosine(session, unit_ids)
                jaccard_matrix = self._compute_entity_jaccard(real_units)
                w_emb = self.retrieval_config.mmr_embedding_weight if self.retrieval_config else 0.6
                w_ent = self.retrieval_config.mmr_entity_weight if self.retrieval_config else 0.4
                sim_matrix = self._build_hybrid_similarity_matrix(
                    cosine_matrix, jaccard_matrix, w_emb, w_ent
                )
                mmr_limit = len(real_units) if token_budget is not None else request.limit
                real_units = self._apply_mmr_diversity(
                    real_units, sim_matrix, mmr_lambda, mmr_limit
                )

            # Re-insert virtual units at their original relative positions
            final_results = list(real_units)
            for orig_pos, vunit in virtual_positions:
                insert_at = min(orig_pos, len(final_results))
                final_results.insert(insert_at, vunit)

        # 10. Collect resonance update info (deferred to background)
        resonance_context: dict[str, Any] | None = None
        if final_results and self.queue_service:
            try:
                retrieved_unit_ids = [u.id for u in final_results]
                stmt = select(UnitEntity.entity_id).where(
                    col(UnitEntity.unit_id).in_(retrieved_unit_ids)
                )
                result = await session.exec(stmt)
                active_entity_ids = set(result.all())
                if active_entity_ids:
                    resonance_context = {
                        'entity_ids': active_entity_ids,
                        'vault_id': primary_vault_id,
                    }
            except (ValueError, RuntimeError, OSError) as e:
                logger.error(f'Failed to collect resonance data: {e}')

        # 10b. Attach debug info to results
        if debug_ctx is not None:
            for unit in final_results:
                info = debug_ctx.per_result.get(unit.id)
                if info:
                    object.__setattr__(unit, '_debug_info', info)

        # 11. Apply Token Budget Filtering
        if token_budget is not None:
            final_results = self._filter_by_token_budget(final_results, token_budget)

        if token_budget is not None:
            return (final_results, resonance_context)
        return (final_results[: request.limit], resonance_context)

    def _fuse_multi_query_results(
        self, ranked_batches: list[tuple[Sequence[Any], float]], limit: int
    ) -> list[Any]:
        """Fuses results from multiple expanded queries using weighted RRF."""
        if len(ranked_batches) == 1:
            return list(ranked_batches[0][0])

        scores: dict[tuple[UUID, str], float] = {}  # (id, type) -> score
        for batch, batch_weight in ranked_batches:
            for rank, item in enumerate(batch):
                key = (item.id, item.type)
                # Weighted RRF: score = sum(weight / (K + rank + 1))
                score = batch_weight / (self.k_rrf + rank + 1)
                scores[key] = scores.get(key, 0.0) + score

        sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)

        from collections import namedtuple

        Item = namedtuple('Item', ['id', 'type'])

        return [Item(id=k[0], type=k[1]) for k in sorted_keys[:limit]]

    def _apply_position_aware_blending(self, results: list[MemoryUnit]) -> list[MemoryUnit]:
        """
        Blends RRF rank and Reranker rank based on position.

        Rank 1-3: 75% retrieval / 25% reranker
        Rank 4-10: 60/40
        Rank 11+: 40/60
        """
        # TODO(T6): This is a NO-OP. Either implement position-aware blending
        # with dual orderings (RRF + reranker) or remove this method. Kept because
        # callers reference `fusion_strategy='position_aware'` in RetrievalRequest.
        return results

    def _resolve_active_strategies(
        self, strategies: list[str] | None
    ) -> tuple[dict[str, tuple[RetrievalStrategy, bool]], bool]:
        """Resolve which strategies to run and whether to include mental models.

        Returns:
            A tuple of (active_unit_strategies, include_mental_model).
        """
        if strategies is None:
            return dict(self.strategies), True

        active = {name: spec for name, spec in self.strategies.items() if name in strategies}
        include_mm = 'mental_model' in strategies
        return active, include_mm

    async def _perform_single_strategy_retrieval(
        self,
        session: AsyncSession,
        query: str,
        query_embedding: list[float],
        limit: int,
        filters: dict[str, Any],
        strategy_name: str,
        strategy: RetrievalStrategy,
        is_desc: bool,
        result_type: str,
    ) -> Sequence[Any]:
        """Fast path: run a single strategy without RRF overhead."""
        stmt = strategy.get_statement(query, query_embedding, limit=limit, **filters)
        subq = stmt.subquery(name=f'sq_{strategy_name}')

        best_score = func.max(subq.c.score).label('best_score')
        rank_order = best_score.desc() if is_desc else best_score.asc()

        final_stmt = (
            select(
                subq.c.id.label('id'),
                literal(result_type).label('type'),
            )
            .select_from(subq)
            .group_by(subq.c.id)
            .order_by(rank_order)
            .limit(limit)
        )

        result = await session.exec(final_stmt)
        return result.all()

    async def _perform_rrf_retrieval(
        self,
        session: AsyncSession,
        query: str,
        query_embedding: list[float],
        limit: int,
        filters: dict[str, Any],
        strategies: list[str] | None = None,
        strategy_weights: dict[str, float] | None = None,
        debug_ctx: DebugContext | None = None,
    ) -> Sequence[Any]:
        """Executes the Reciprocal Rank Fusion query with optional strategy filtering."""
        active_strategies, include_mm = self._resolve_active_strategies(strategies)

        total_active = len(active_strategies) + (1 if include_mm else 0)

        # Single-strategy fast path: skip RRF entirely (disabled when debug is on)
        if total_active == 1 and debug_ctx is None:
            if active_strategies:
                name, (strategy, is_desc) = next(iter(active_strategies.items()))
                return await self._perform_single_strategy_retrieval(
                    session,
                    query,
                    query_embedding,
                    limit,
                    filters,
                    name,
                    strategy,
                    is_desc,
                    'unit',
                )
            else:
                # Only mental_model
                return await self._perform_single_strategy_retrieval(
                    session,
                    query,
                    query_embedding,
                    limit,
                    filters,
                    'mental_model',
                    self.mm_strategy,
                    False,
                    'model',
                )

        # Debug path: run strategies individually to capture per-result attribution
        if debug_ctx is not None:
            weights = strategy_weights or {}
            return await self._perform_rrf_retrieval_debug(
                session,
                query,
                query_embedding,
                limit,
                filters,
                active_strategies,
                include_mm,
                weights,
                debug_ctx,
            )

        # Multi-strategy path: build CTEs with weighted RRF
        weights = strategy_weights or {}
        ctes = []

        # Memory Strategies
        pool_size = self.candidate_pool_size
        for name, (strategy, is_desc) in active_strategies.items():
            weight = weights.get(name, 1.0)
            stmt = strategy.get_statement(query, query_embedding, limit=pool_size, **filters)
            subq = stmt.subquery(name=f'sq_{name}')
            rank_order = subq.c.score.desc() if is_desc else subq.c.score.asc()

            cte = (
                select(
                    subq.c.id,
                    literal('unit').label('type'),
                    func.rank().over(order_by=rank_order).label('rnk'),
                    literal(weight).label('weight'),
                )
                .select_from(subq)
                .cte(f'cte_{name}')
            )
            ctes.append(cte)

        # Mental Model Strategy
        if include_mm:
            mm_weight = weights.get('mental_model', 1.0)
            mm_stmt = self.mm_strategy.get_statement(
                query, query_embedding, limit=pool_size, **filters
            )
            mm_subq = mm_stmt.subquery(name='sq_mental_model')
            mm_cte = (
                select(
                    mm_subq.c.id,
                    literal('model').label('type'),
                    func.rank().over(order_by=mm_subq.c.score.asc()).label('rnk'),
                    literal(mm_weight).label('weight'),
                )
                .select_from(mm_subq)
                .cte('cte_mental_model')
            )
            ctes.append(mm_cte)

        # Union and Score
        union_query = union_all(*[select(c.c.id, c.c.type, c.c.rnk, c.c.weight) for c in ctes])
        candidates_cte = union_query.cte('all_candidates')

        rrf_score = func.sum(candidates_cte.c.weight / (self.k_rrf + candidates_cte.c.rnk)).label(
            'rrf_score'
        )
        scores_cte = (
            select(candidates_cte.c.id, candidates_cte.c.type, rrf_score)
            .select_from(candidates_cte)
            .group_by(candidates_cte.c.id, candidates_cte.c.type)
        ).cte('final_scores')

        final_stmt = (
            select(scores_cte.c.id.label('id'), scores_cte.c.type.label('type'))
            .select_from(scores_cte)
            .order_by(scores_cte.c.rrf_score.desc())
            .limit(limit)
        )

        result = await session.exec(final_stmt)
        return result.all()

    async def _perform_partitioned_rrf(
        self,
        session: AsyncSession,
        query: str,
        query_embedding: list[float],
        limit: int,
        filters: dict[str, Any],
        strategies: list[str] | None = None,
        strategy_weights: dict[str, float] | None = None,
        debug_ctx: DebugContext | None = None,
    ) -> Sequence[Any]:
        """Run RRF independently per fact type, then interleave results.

        Each fact type (from ``FactTypes``) gets its own RRF pass limited to
        ``RetrievalConfig.fact_type_budget`` candidates. Results are merged via
        round-robin interleaving across type buckets (plus mental models as an
        extra bucket when enabled).

        Enabled by ``RetrievalConfig.fact_type_partitioned_rrf``.
        """
        fact_types = [ft.value for ft in FactTypes]
        per_type_budget = self.retrieval_config.fact_type_budget

        # Resolve which strategies to run so we can handle mental models separately
        active_strategies, include_mm = self._resolve_active_strategies(strategies)

        # If mental_model was explicitly requested, exclude it from unit strategies list
        unit_only_strategies = (
            [s for s in (strategies or []) if s != 'mental_model']
            if strategies is not None
            else None
        )

        # Parallel path: create a separate session per fact type to avoid
        # AsyncSession concurrency issues.  Falls back to sequential if no
        # session factory is available.
        if self._session_factory is not None:
            sf = self._session_factory

            async def _run_ft(ft: str) -> Sequence[Any]:
                async with sf() as ft_session:
                    return await self._perform_rrf_retrieval(
                        ft_session,
                        query,
                        query_embedding,
                        per_type_budget,
                        {**filters, 'fact_type': ft},
                        strategies=unit_only_strategies,
                        strategy_weights=strategy_weights,
                        debug_ctx=debug_ctx,
                    )

            per_type_results = list(await asyncio.gather(*[_run_ft(ft) for ft in fact_types]))
        else:
            # Sequential fallback (no session factory)
            per_type_results = []
            for ft in fact_types:
                result = await self._perform_rrf_retrieval(
                    session,
                    query,
                    query_embedding,
                    per_type_budget,
                    {**filters, 'fact_type': ft},
                    strategies=unit_only_strategies,
                    strategy_weights=strategy_weights,
                    debug_ctx=debug_ctx,
                )
                per_type_results.append(result)

        # Collect mental model results separately (mental models have no fact_type)
        mm_results: Sequence[Any] = []
        if include_mm:
            mm_items = await self._perform_rrf_retrieval(
                session,
                query,
                query_embedding,
                per_type_budget,
                filters,
                strategies=['mental_model'],
                strategy_weights=strategy_weights,
                debug_ctx=debug_ctx,
            )
            mm_results = mm_items

        # Interleave: round-robin across fact types
        merged: list[Any] = []
        seen: set[UUID] = set()

        # Include mental model results as an additional "type bucket"
        all_buckets: list[Sequence[Any]] = list(per_type_results)
        if mm_results:
            all_buckets.append(mm_results)

        max_len = max((len(r) for r in all_buckets), default=0)
        for i in range(max_len):
            for results in all_buckets:
                if i < len(results) and results[i].id not in seen:
                    merged.append(results[i])
                    seen.add(results[i].id)
                    if len(merged) >= limit:
                        break
            if len(merged) >= limit:
                break

        return merged[:limit]

    async def _perform_rrf_retrieval_debug(
        self,
        session: AsyncSession,
        query: str,
        query_embedding: list[float],
        limit: int,
        filters: dict[str, Any],
        active_strategies: dict[str, tuple[RetrievalStrategy, bool]],
        include_mm: bool,
        weights: dict[str, float],
        debug_ctx: DebugContext,
    ) -> Sequence[Any]:
        """
        Debug variant of RRF retrieval: runs strategies individually to capture
        per-strategy timing and per-result rank attribution. Produces the same
        RRF-fused ranking as the SQL CTE path.
        """
        from collections import namedtuple

        Item = namedtuple('Item', ['id', 'type'])

        pool_size = self.candidate_pool_size
        all_strategy_rows: list[tuple[str, float, str, list[tuple[UUID, int, float | None]]]] = []

        for name, (strategy, is_desc) in active_strategies.items():
            weight = weights.get(name, 1.0)
            stmt = strategy.get_statement(query, query_embedding, limit=pool_size, **filters)
            subq = stmt.subquery(name=f'sq_{name}')
            rank_order = subq.c.score.desc() if is_desc else subq.c.score.asc()

            timed_stmt = select(
                subq.c.id,
                subq.c.score,
                func.rank().over(order_by=rank_order).label('rnk'),
            ).select_from(subq)

            t0 = time.monotonic()
            result = await session.exec(timed_stmt)
            rows = result.all()
            elapsed_ms = (time.monotonic() - t0) * 1000
            debug_ctx.strategy_timings[name] = elapsed_ms

            parsed = [
                (r.id, int(r.rnk), float(r.score) if r.score is not None else None) for r in rows
            ]
            all_strategy_rows.append((name, weight, 'unit', parsed))

        if include_mm:
            mm_weight = weights.get('mental_model', 1.0)
            mm_stmt = self.mm_strategy.get_statement(
                query, query_embedding, limit=pool_size, **filters
            )
            mm_subq = mm_stmt.subquery(name='sq_mental_model')

            timed_stmt = select(
                mm_subq.c.id,
                mm_subq.c.score,
                func.rank().over(order_by=mm_subq.c.score.asc()).label('rnk'),
            ).select_from(mm_subq)

            t0 = time.monotonic()
            result = await session.exec(timed_stmt)
            rows = result.all()
            elapsed_ms = (time.monotonic() - t0) * 1000
            debug_ctx.strategy_timings['mental_model'] = elapsed_ms

            parsed = [
                (r.id, int(r.rnk), float(r.score) if r.score is not None else None) for r in rows
            ]
            all_strategy_rows.append(('mental_model', mm_weight, 'model', parsed))

        rrf_scores: dict[tuple[UUID, str], float] = {}

        for strategy_name, weight, result_type, rows in all_strategy_rows:
            timing = debug_ctx.strategy_timings.get(strategy_name)
            for uid, rank, raw_score in rows:
                key = (uid, result_type)
                rrf_contribution = weight / (self.k_rrf + rank)
                rrf_scores[key] = rrf_scores.get(key, 0.0) + rrf_contribution

                debug_ctx.per_result[uid].append(
                    StrategyContribution(
                        strategy_name=strategy_name,
                        rank=rank,
                        rrf_score=round(rrf_contribution, 6),
                        raw_score=(round(raw_score, 6) if raw_score is not None else None),
                        timing_ms=(round(timing, 2) if timing is not None else None),
                    )
                )

        sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)
        return [Item(id=k[0], type=k[1]) for k in sorted_keys[:limit]]

    async def _hydrate_results(
        self, session: AsyncSession, ranked_items: Sequence[Any]
    ) -> list[MemoryUnit]:
        """Fetches actual objects from DB and converts them to MemoryUnits."""
        unit_ids = [row.id for row in ranked_items if row.type == 'unit']
        model_ids = [row.id for row in ranked_items if row.type == 'model']

        fetched_units = {}
        fetched_models = {}

        if unit_ids:
            units = (
                await session.exec(
                    select(MemoryUnit)
                    .where(col(MemoryUnit.id).in_(unit_ids))
                    .options(defer(MemoryUnit.embedding))  # type: ignore
                    .options(selectinload(MemoryUnit.note))
                    .options(selectinload(MemoryUnit.unit_entities))
                )
            ).all()
            fetched_units = {u.id: u for u in units}

        if model_ids:
            models = (
                await session.exec(
                    select(MentalModel)
                    .where(col(MentalModel.id).in_(model_ids))
                    .options(defer(MentalModel.embedding))  # type: ignore
                )
            ).all()
            fetched_models = {m.id: m for m in models}

        # Load supersession context for low-confidence units
        low_conf_ids = [u.id for u in fetched_units.values() if getattr(u, 'confidence', 1.0) < 1.0]
        if low_conf_ids:
            from memex_core.memory.sql_models import MemoryLink

            link_stmt = select(MemoryLink).where(
                col(MemoryLink.to_unit_id).in_(low_conf_ids),
                col(MemoryLink.link_type).in_(['contradicts', 'weakens']),
            )
            link_result = await session.exec(link_stmt)
            links_by_target: dict[UUID, list[MemoryLink]] = defaultdict(list)
            for link in link_result.all():
                links_by_target[link.to_unit_id].append(link)

            for uid, links_list in links_by_target.items():
                if uid in fetched_units:
                    unit = fetched_units[uid]
                    supersession_info = []
                    for link in links_list:
                        auth_id = (
                            UUID(link.link_metadata.get('authoritative_unit_id', ''))
                            if link.link_metadata
                            and link.link_metadata.get('authoritative_unit_id')
                            else link.from_unit_id
                        )
                        auth_unit = fetched_units.get(auth_id)
                        auth_text = auth_unit.text if auth_unit else ''
                        note_title = (
                            link.link_metadata.get('superseding_note_title')
                            if link.link_metadata
                            else None
                        )
                        supersession_info.append(
                            {
                                'unit_id': str(auth_id),
                                'unit_text': auth_text[:200],
                                'note_title': note_title,
                                'relation': link.link_type,
                            }
                        )
                    unit.unit_metadata['superseded_by'] = supersession_info

        final_results = []
        for row in ranked_items:
            if row.type == 'unit' and row.id in fetched_units:
                final_results.append(fetched_units[row.id])
            elif row.type == 'model' and row.id in fetched_models:
                final_results.extend(self._convert_mm_to_units(fetched_models[row.id]))

        return final_results

    async def _rerank_results(
        self, query: str, results: list[MemoryUnit], min_score: float | None = None
    ) -> list[MemoryUnit]:
        """Re-rank results using a cross-encoder with multiplicative boosts.

        Applies sigmoid-normalized cross-encoder scores, then multiplies by:
        * **recency boost** -- scaled by ``RetrievalConfig.reranking_recency_alpha``
          (linear decay over 365 days)
        * **temporal proximity boost** -- scaled by
          ``RetrievalConfig.reranking_temporal_alpha`` (uses ``unit.temporal_proximity``
          when available)

        Set both alphas to 0 to disable boosts (backward compatible).
        """
        if not self.reranker or not results:
            return results

        try:
            formatted_texts = []
            for unit in results:
                # Use event_date, fallback to created_at or now if absolutely necessary
                dt = unit.event_date or unit.created_at or datetime.now(timezone.utc)
                formatted_texts.append(
                    format_for_reranking(
                        text=unit.text,
                        event_date=dt,
                        fact_type=unit.fact_type,
                        context=unit.context,
                    )
                )

            scores = await asyncio.to_thread(self.reranker.score, query, formatted_texts)

            # Normalize cross-encoder scores to [0, 1] via sigmoid
            normalized_scores = [1.0 / (1.0 + math.exp(-s)) for s in scores]

            # Apply multiplicative recency and temporal proximity boosts
            now = datetime.now(timezone.utc)
            recency_alpha = self.retrieval_config.reranking_recency_alpha
            temporal_alpha = self.retrieval_config.reranking_temporal_alpha

            boosted_scores: list[float] = []
            for unit, ce_score in zip(results, normalized_scores):
                # Recency boost
                if unit.event_date is not None:
                    days_ago = (now - unit.event_date).days
                    recency = max(0.1, min(1.0, 1.0 - (days_ago / 365)))
                else:
                    recency = 0.5  # neutral when no event_date

                recency_boost = 1.0 + recency_alpha * (recency - 0.5)

                # Temporal proximity boost
                temporal: float | None = getattr(unit, 'temporal_proximity', None)
                if temporal is None:
                    temporal = 0.5  # neutral
                temporal_boost = 1.0 + temporal_alpha * (temporal - 0.5)

                boosted_scores.append(ce_score * recency_boost * temporal_boost)

            scored_results = []
            for unit, boosted, raw_score in zip(results, boosted_scores, scores):
                # Apply sigmoid threshold on raw score if requested
                if min_score is not None:
                    prob = 1.0 / (1.0 + math.exp(-raw_score))
                    if prob < min_score:
                        continue
                scored_results.append((unit, boosted))

            scored_results.sort(key=lambda x: x[1], reverse=True)
            return [item[0] for item in scored_results]
        except (ValueError, RuntimeError, OSError) as e:
            logger.error(f'Reranking failed: {e}. Falling back to RRF order.')
            return results

    @staticmethod
    async def _compute_pairwise_cosine(
        session: AsyncSession, unit_ids: list[UUID]
    ) -> dict[tuple[UUID, UUID], float]:
        """Compute pairwise cosine similarity for a set of memory units via SQL."""
        from sqlalchemy import text

        if len(unit_ids) < 2:
            return {}

        stmt = text("""
            WITH reps AS (
                SELECT id, embedding
                FROM memory_units
                WHERE id = ANY(:unit_ids)
            )
            SELECT a.id AS id_a, b.id AS id_b,
                   1 - (a.embedding <=> b.embedding) AS similarity
            FROM reps a
            CROSS JOIN reps b
            WHERE a.id < b.id
        """)
        result = await session.execute(stmt, {'unit_ids': [str(uid) for uid in unit_ids]})
        matrix: dict[tuple[UUID, UUID], float] = {}
        for row in result:
            key = (row.id_a, row.id_b)
            matrix[key] = float(row.similarity)
            matrix[(row.id_b, row.id_a)] = float(row.similarity)
        return matrix

    @staticmethod
    def _compute_entity_jaccard(results: list[MemoryUnit]) -> dict[tuple[UUID, UUID], float]:
        """Compute pairwise entity Jaccard similarity from eagerly-loaded unit_entities."""
        entity_sets: dict[UUID, set[UUID]] = {}
        for unit in results:
            entity_sets[unit.id] = {ue.entity_id for ue in (unit.unit_entities or [])}

        matrix: dict[tuple[UUID, UUID], float] = {}
        ids = [u.id for u in results]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a_set, b_set = entity_sets[ids[i]], entity_sets[ids[j]]
                union = a_set | b_set
                if not union:
                    sim = 0.0
                else:
                    sim = len(a_set & b_set) / len(union)
                matrix[(ids[i], ids[j])] = sim
                matrix[(ids[j], ids[i])] = sim
        return matrix

    @staticmethod
    def _build_hybrid_similarity_matrix(
        cosine_matrix: dict[tuple[UUID, UUID], float],
        jaccard_matrix: dict[tuple[UUID, UUID], float],
        w_emb: float,
        w_ent: float,
    ) -> dict[tuple[UUID, UUID], float]:
        """Combine cosine and entity Jaccard into a hybrid similarity matrix."""
        all_pairs = set(cosine_matrix.keys()) | set(jaccard_matrix.keys())
        matrix: dict[tuple[UUID, UUID], float] = {}
        for pair in all_pairs:
            cos = cosine_matrix.get(pair, 0.0)
            jac = jaccard_matrix.get(pair, 0.0)
            matrix[pair] = w_emb * cos + w_ent * jac
        return matrix

    @staticmethod
    def _apply_mmr_diversity(
        results: list[MemoryUnit],
        similarity_matrix: dict[tuple[UUID, UUID], float],
        lambda_: float,
        limit: int,
    ) -> list[MemoryUnit]:
        """Greedy MMR selection with temporal tiebreaker."""
        if not results:
            return results

        n = len(results)
        # Relevance is positional score from current ordering
        relevance = {results[i].id: (n - i) / n for i in range(n)}

        selected: list[MemoryUnit] = []
        remaining = list(results)

        # First item is always the top result
        selected.append(remaining.pop(0))

        eps = 0.01  # Tiebreaker threshold

        while remaining and len(selected) < limit:
            best_score = -float('inf')
            best_idx = 0

            for idx, candidate in enumerate(remaining):
                rel = relevance[candidate.id]
                # Max similarity to any already-selected item
                max_sim = 0.0
                for sel in selected:
                    pair = (candidate.id, sel.id)
                    max_sim = max(max_sim, similarity_matrix.get(pair, 0.0))
                mmr_score = lambda_ * rel - (1 - lambda_) * max_sim

                if mmr_score > best_score + eps:
                    best_score = mmr_score
                    best_idx = idx
                elif abs(mmr_score - best_score) <= eps:
                    # Temporal tiebreaker: prefer newer event_date
                    current_best = remaining[best_idx]
                    _min = datetime.min
                    if (candidate.event_date or _min) > (current_best.event_date or _min):
                        best_score = mmr_score
                        best_idx = idx

            selected.append(remaining.pop(best_idx))

        return selected

    def _attach_citations(self, units: list[MemoryUnit]) -> list[MemoryUnit]:
        """
        Identify 'Observation' units and their evidence.
        Attach citation metadata to observations that reference facts in the result set.
        Both facts and observations remain in the results — the reranker decides relevance
        and MMR handles diversity.
        """
        unit_map = {u.id: u for u in units}

        for unit in units:
            evidence_ids = unit.unit_metadata.get('evidence_ids', []) or []
            if not isinstance(evidence_ids, list):
                evidence_ids = []

            supporting_ids = (
                unit.unit_metadata.get('supporting_evidence_ids')
                or unit.unit_metadata.get('evidence_indices')
                or []
            )
            if isinstance(supporting_ids, list):
                evidence_ids.extend(supporting_ids)

            if evidence_ids:
                citations = []
                for evid_raw in evidence_ids:
                    try:
                        evid = UUID(str(evid_raw))
                    except (ValueError, TypeError):
                        continue

                    if evid in unit_map and evid != unit.id:
                        cited_unit = unit_map[evid]
                        citations.append(cited_unit)

                if citations:
                    existing_citations = unit.unit_metadata.get('citations', [])
                    new_citations = [
                        {
                            'text': c.text,
                            'date': c.event_date.isoformat() if c.event_date else None,
                            'id': str(c.id),
                        }
                        for c in citations
                    ]
                    unit.unit_metadata['citations'] = existing_citations + new_citations

        return units

    def _convert_mm_to_units(self, model: MentalModel) -> list[MemoryUnit]:
        """Converts a MentalModel into virtual MemoryUnits for observations."""
        from memex_core.memory.reflect.trends import compute_trend

        units = []
        for obs in model.observations:
            # Handle both dicts (from JSONB) and objects (in-memory)
            if isinstance(obs, dict):
                title = obs.get('title', 'Observation')
                content = obs.get('content', '')
                evidence = obs.get('evidence', [])
            else:
                title = getattr(obs, 'title', 'Observation')
                content = getattr(obs, 'content', '')
                evidence = getattr(obs, 'evidence', [])

            trend = compute_trend(evidence)
            evidence_ids = []
            for item in evidence:
                if isinstance(item, dict):
                    mid = item.get('memory_id')
                else:
                    mid = getattr(item, 'memory_id', None)
                if mid:
                    evidence_ids.append(str(mid))

            virtual_id = UUID(int=hash(str(model.id) + title) & (2**128 - 1))
            units.append(
                MemoryUnit(
                    id=virtual_id,
                    text=f'[{model.name}] {title}: {content}',
                    fact_type=FactTypes.OBSERVATION,
                    status=ContentStatus.ACTIVE,
                    event_date=model.last_refreshed,
                    vault_id=model.vault_id,
                    note_id=model.id,
                    embedding=[],
                    unit_metadata={
                        'observation': True,
                        'virtual': True,
                        'trend': str(trend.value) if hasattr(trend, 'value') else str(trend),
                        'evidence_ids': evidence_ids,
                    },
                )
            )
        return units

    def _filter_by_token_budget(self, units: list[MemoryUnit], budget: int) -> list[MemoryUnit]:
        """
        Greedily pack facts into the result set until the cumulative token count reaches budget.
        Implements Equation 17 from the Hindsight paper.
        Uses tiktoken for accurate counting.
        """
        if not units:
            return []

        final_set = []
        cumulative_tokens = 0

        encoding = tiktoken.get_encoding('cl100k_base')

        for unit in units:
            # Count tokens for the unit text
            tokens = encoding.encode(unit.text)
            count = len(tokens)

            if cumulative_tokens + count <= budget:
                final_set.append(unit)
                cumulative_tokens += count
            else:
                # Once we hit the budget, we stop (Greedy packing per paper)
                break

        return final_set
