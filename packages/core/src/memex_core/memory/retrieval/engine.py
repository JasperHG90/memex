import hashlib
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID, uuid5
import asyncio
from datetime import datetime, timezone
from typing import Any, Sequence
import math

import numpy as np
import tiktoken
from cachetools import TTLCache
from sqlalchemy import func, literal, text, union_all
from sqlalchemy.orm import defer, selectinload
from sqlalchemy.sql.elements import TextClause
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import RetrievalConfig, ReflectionConfig
from memex_core.memory.models.protocols import EmbeddingsModel, RerankerModel
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.reranking import get_reranking_model
from memex_core.memory.models.ner import FastNERModel, get_ner_model
from memex_core.memory.retrieval.strategies import (
    KeywordStrategy,
    RetrievalStrategy,
    SemanticStrategy,
    TemporalStrategy,
    MentalModelStrategy,
    get_graph_strategy,
)
from memex_core.instrument import _instrument
from memex_core.memory.retrieval.expansion import QueryExpander
from memex_core.memory.retrieval._offload import (
    get_embedding_call_timeout,
    get_embedding_semaphore,
    get_ner_call_timeout,
    get_ner_semaphore,
    get_reranker_call_timeout,
    get_reranker_semaphore,
)
from memex_core.memory.retrieval.rerank_cache import CrossEncoderScoreCache, hash_query
from memex_core.memory.retrieval.temporal_extraction import extract_temporal_constraint
from memex_core.memory.retrieval.temporal_concretizer import (
    TemporalConcretizer,
    has_ambiguous_temporal_expression,
)
from memex_core.memory.sql_models import (
    MemoryUnit,
    MentalModel,
    UnitEntity,
    ContentStatus,
    Vault,
    MWMode,
)
from memex_core.memory.retrieval.models import RetrievalRequest
from memex_common.types import FactTypes
from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.confidence import (
    HasConfidence,
    certainty_from_variance,
    extract_confidence_and_count,
    mean_and_variance,
)
from memex_core.memory.formatting import format_for_reranking
from memex_core.metrics import (
    COMPOSITE_BOOST_CLIPPED,
    COMPOSITE_BOOST_NAN_GUARD_TRIGGERED,
    CONFIDENCE_BOOST_OBSERVED,
    CONFIDENCE_SCORE_DISTRIBUTION,
    CONFIDENCE_VARIANCE_OBSERVED,
    CROSS_ENCODER_INPUT_COUNT_HISTOGRAM,
    DECAY_BOOST_OBSERVED,
    MW_BOOST_OBSERVED,
)

logger = logging.getLogger('memex.core.memory.retrieval.engine')

LOG_FLOOR_COMPOSITE_BOOST = 1e-9


def _compose_boosts_logspace(
    ce_score: float,
    *,
    recency: float,
    temporal: float,
    mw: float,
    confidence: float,
    decay: float,
    log_clip: float,
) -> float:
    """Compose five boost factors onto ce_score with log-space additive clip.

    Returns ``ce_score * exp(clip(sum(log(b_i)), -log_clip, +log_clip))``.
    Boost values at or below ``LOG_FLOOR_COMPOSITE_BOOST = 1e-9`` are floored
    at the floor before taking the log, so zero or (theoretically) negative
    inputs do not raise. At the ship default ``log_clip = math.inf`` the clip
    is a no-op and the result equals the prior multiplicative product for
    strictly positive boost inputs; for an input that hits the floor (e.g. a
    boost of exactly ``0.0``), the new form produces ``ce_score * ~1e-9`` for
    a single zero factor (``ce_score * 1e-9^k`` if ``k`` factors hit the
    floor; degenerate all-five case is ``ce_score * 1e-45``) rather than the
    strict ``0.0`` the multiplicative product would return. Rank-equivalent
    for retrieval scoring (both at the bottom), but no longer ties every
    zero-boost unit at zero. Under ship-default alphas every boost evaluates
    to ``1.0`` (or strictly positive), so the zero-input path is dormant.

    Any ``NaN`` input (boost, ``ce_score``, or ``log_clip``) short-circuits:
    the function returns ``ce_score`` unmodified and emits a separate
    ``COMPOSITE_BOOST_NAN_GUARD_TRIGGERED`` counter increment instead of
    observing ``1.0`` to the regular histogram (which would be
    indistinguishable from a genuine neutral multiplier).

    Emits the post-clip multiplier to ``COMPOSITE_BOOST_CLIPPED`` for
    operator visibility into whether the clip is firing in production
    traffic.
    """
    if (
        not math.isfinite(ce_score)
        or any(not math.isfinite(b) for b in (recency, temporal, mw, confidence, decay))
        or math.isnan(log_clip)
    ):
        COMPOSITE_BOOST_NAN_GUARD_TRIGGERED.inc()
        return ce_score
    log_boost = (
        math.log(max(recency, LOG_FLOOR_COMPOSITE_BOOST))
        + math.log(max(temporal, LOG_FLOOR_COMPOSITE_BOOST))
        + math.log(max(mw, LOG_FLOOR_COMPOSITE_BOOST))
        + math.log(max(confidence, LOG_FLOOR_COMPOSITE_BOOST))
        + math.log(max(decay, LOG_FLOOR_COMPOSITE_BOOST))
    )
    clipped = max(-log_clip, min(log_clip, log_boost))
    multiplier = math.exp(clipped)
    COMPOSITE_BOOST_CLIPPED.observe(multiplier)
    return ce_score * multiplier


def _get_confidence(unit: HasConfidence) -> float:
    """Resolve a unit's confidence with defensive fallback to 1.0.

    Thin wrapper over the shared
    :func:`memex_core.memory.confidence.extract_confidence_and_count` so the
    falsy-zero handling and ``None`` fallback stay in one place. Schema is
    NOT NULL DEFAULT 1.0; this guard handles stripped/stale model objects
    (no attribute) and rows materialised with confidence=None before the
    column default takes effect. ``0.0`` is a legitimate value (unit
    contradicted to zero) and is preserved verbatim — never coerced via
    ``or 1.0``.
    """
    confidence, _ = extract_confidence_and_count(unit)
    return confidence


# Pre-reranker filter at hydration.
# STABILITY_SECONDS_PER_DAY is re-exported from
# memex_core.memory.retrieval.constants so the SQL builder and the Python
# boost share one source of truth. Numeric values flow into SQL via
# parameter binding (asyncpg ``$N`` placeholder), never f-string
# interpolation — see _build_pre_filter_clause's SECURITY INVARIANT.
from memex_core.memory.retrieval.constants import (  # noqa: E402
    STABILITY_SECONDS_PER_DAY,
    STABILITY_THRESHOLD,
)


def _build_pre_filter_clause(
    *,
    apply_pre_filter: bool,
    fsfm_branch_enabled: bool,
) -> TextClause | None:
    """Build the pre-reranker predicate as a SQLAlchemy ``TextClause``.

    Returns a ``TextClause`` already wrapped as ``NOT (...)`` so the caller
    can pass it straight to ``stmt.where(...)``, or ``None`` when the
    entire pre-filter must drop out (``apply_pre_filter=False`` or no
    branches active).

    SECURITY / parameter-binding INVARIANT: every numeric this builder
    substitutes flows through ``bindparams(...)`` (asyncpg ``$N``
    placeholder), NOT f-string interpolation — regardless of provenance.
    The current FSFM branch reads ``STABILITY_SECONDS_PER_DAY`` and
    ``STABILITY_THRESHOLD`` from
    ``memex_core.memory.retrieval.constants``, but the convention applies
    uniformly: the next iteration of this builder may take per-vault or
    per-class overrides that ARE user-controlled, and routing some values
    through interpolation while others use binding creates an inconsistent
    surface where the wrong code path can leak. Pinning test asserts the
    rendered SQL string is independent of the constants' values.

    Implementation pitfall: the FSFM branch is included via a **Python-level
    conditional**, NOT a SQL-side runtime flag like ``(NOT :fsfm_enabled OR
    ...)``. SQL-side guards still reference the missing column names at parse
    time and would crash on ``column "importance" does not exist`` until the
    FSFM migration runs.

    The pinning test in
    ``tests/unit/retrieval/test_f40_sql_builder.py`` enforces this by
    asserting that the generated SQL string does not contain
    ``importance`` / ``stability`` / ``last_outcome_at`` substrings when
    ``fsfm_branch_enabled=False``.
    """
    if not apply_pre_filter:
        return None

    branches: list[str] = []
    binds: dict[str, float] = {}

    # Memory Worth branch (always on — columns exist since the outcomes feature).
    # Beta-Bernoulli α=β=1 closed form: mw_score = (succ + 1) / (succ + fail + 2)
    branches.append(
        '((memory_units.success_co_count + memory_units.failure_co_count) >= 5 '
        'AND (memory_units.success_co_count + 1.0) / '
        '(memory_units.success_co_count + memory_units.failure_co_count + 2.0) < 0.15)'
    )

    # FSFM branch (gated by config flag — columns ship with the FSFM migration).
    # COALESCE(..., FALSE) wraps the *branch result*, not individual columns,
    # so SQL three-valued logic ``FALSE OR NULL OR FALSE -> NULL`` doesn't
    # poison the surrounding ``NOT`` and exclude cold-start rows. NULLIF on
    # ``stability`` keeps zero-stability rows from filtering (degenerate
    # state — observability surfaces it). STABILITY_SECONDS_PER_DAY and
    # STABILITY_THRESHOLD are bound parameters (see invariant above).
    if fsfm_branch_enabled:
        branches.append(
            'COALESCE('
            'memory_units.importance * '
            'exp(-EXTRACT(EPOCH FROM (now() - memory_units.last_outcome_at)) / '
            ':stability_seconds_per_day / NULLIF(memory_units.stability, 0)) '
            '< :stability_threshold, '
            'FALSE)'
        )
        binds['stability_seconds_per_day'] = STABILITY_SECONDS_PER_DAY
        binds['stability_threshold'] = STABILITY_THRESHOLD

    # Confidence branch (always on; column is NOT NULL DEFAULT 1.0,
    # so cold-start units never match). Strict ``<`` keeps the 0.2 boundary
    # safe. No COALESCE wrap needed: unlike FSFM, ``confidence`` cannot be
    # NULL by schema, so SQL three-valued logic does not arise. The
    # contradiction engine's α-stepping is itself the evidence-accumulation
    # threshold — adding a separate count gate would double-count.
    branches.append('(memory_units.confidence < 0.2)')

    if not branches:
        return None

    # OR'd, not AND'd — either signal is sufficient grounds to skip the
    # cross-encoder. Cold-start safeguards (Memory Worth >= 5 outcomes, FSFM exp(elapsed))
    # are inside the individual branches.
    pre_filter_clause = ' OR '.join(branches)
    clause = text(f'NOT ({pre_filter_clause})')
    if binds:
        clause = clause.bindparams(**binds)
    return clause


def derive_note_status(units: list[MemoryUnit], superseded_threshold: float = 0.3) -> str:
    """Derive note-level status from unit confidences."""
    if not units:
        return 'active'
    low_confidence = sum(
        1 for u in units if extract_confidence_and_count(u)[0] < superseded_threshold
    )
    ratio = low_confidence / len(units)
    if ratio > 0.5:
        return 'superseded'
    elif low_confidence > 0:
        return 'partially_superseded'
    return 'active'


# RRF Constant
K_RRF = 60
CANDIDATE_POOL_SIZE = 60

# Namespace for deterministic virtual MemoryUnit ids synthesized from
# MentalModel observations. Stable across processes, unlike Python's
# PYTHONHASHSEED-salted hash(). Reproducible via:
#   uuid5(uuid.NAMESPACE_URL, 'memex/retrieval/virtual-mental-model-observation')
_VIRTUAL_UNIT_NS = UUID('bf63d7e5-1e6a-5cf4-9f33-2e85d3a48d38')


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
    embedder: EmbeddingsModel | None = None,
    reranker: RerankerModel | None = None,
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
            _retrieval_cfg = retrieval_config or RetrievalConfig()
            reranker = await get_reranking_model(
                batch_size=_retrieval_cfg.reranker_batch_size,
            )
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
        embedder: EmbeddingsModel,
        reranker: RerankerModel | None = None,
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
        # Per-engine RNG for the ε-greedy roll. ``random.random()``
        # would dispatch to CPython's module-global Mersenne Twister,
        # which is guarded by a lock — under high concurrency that lock
        # is contended. A per-instance ``random.Random()`` is unshared
        # and lock-free, and keeps the global RNG free for callers that
        # rely on it being seedable from outside.
        self._rng = random.Random()
        self.expander = QueryExpander(lm=self.lm) if self.lm else None
        self.concretizer: TemporalConcretizer | None = (
            TemporalConcretizer(lm=self.lm)
            if self.lm and self.retrieval_config.temporal_concretization_enabled
            else None
        )
        self._session_factory = session_factory

        # Anisotropy corrector — shared across retrieval / contradiction /
        # extraction-dedup since they observe the same embedding manifold.
        # Disabled mode keeps a private instance so the singleton stays
        # untainted for other callers.
        from memex_core.memory.models.anisotropy import (
            AnisotropyCorrector,
            get_shared_corrector,
        )

        if self.retrieval_config.anisotropy_window_size == 0:
            self._anisotropy = AnisotropyCorrector(window_size=0)
        else:
            self._anisotropy = get_shared_corrector(
                window_size=self.retrieval_config.anisotropy_window_size,
                min_samples=self.retrieval_config.anisotropy_min_samples,
            )

        # Source RRF constants from config
        self.k_rrf = self.retrieval_config.rrf_k
        self.candidate_pool_size = self.retrieval_config.candidate_pool_size

        # Query embedding cache: avoids re-encoding recently seen queries
        self._embedding_cache: TTLCache[str, np.ndarray] = TTLCache(maxsize=256, ttl=300)
        self._embedding_cache_lock = asyncio.Lock()

        # Cross-encoder score cache (default-on; bypassed when flag is False)
        self._rerank_cache: CrossEncoderScoreCache | None = (
            CrossEncoderScoreCache(
                max_size=self.retrieval_config.cross_encoder_cache_size,
                ttl_seconds=self.retrieval_config.cross_encoder_cache_ttl_seconds,
            )
            if self.retrieval_config.cross_encoder_cache_enabled
            else None
        )

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
            # Shared embedding cap across api.py + document_search.py +
            # retrieval/engine.py — one model, one capacity budget. Thread
            # keeps running on timeout; cap prevents thread accumulation.
            async with get_embedding_semaphore(), _instrument('embed'):
                encoded = await asyncio.wait_for(
                    asyncio.to_thread(self.embedder.encode, miss_texts),
                    timeout=get_embedding_call_timeout(),
                )
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
        _t = time.monotonic

        # 1. Query Expansion (Multi-Query)
        t0 = _t()
        queries = [request.query]
        query_weights = [2.0]  # Original query is weighted higher

        primary_vault_id = request.vault_ids[0] if request.vault_ids else GLOBAL_VAULT_ID

        if request.expand_query and self.expander:
            variations = await self.expander.expand(request.query)
            for var in variations:
                queries.append(var)
                query_weights.append(1.0)
        t_expand = _t() - t0

        # 2. Get Embeddings for all queries (with per-query caching)
        t0 = _t()
        all_embeddings = await self._get_embeddings_cached(queries)
        t_embed = _t() - t0

        # 3. Determine budget and limit
        token_budget = request.token_budget
        if token_budget is None and self.retrieval_config:
            token_budget = self.retrieval_config.token_budget
        if token_budget is not None and token_budget <= 0:
            token_budget = None

        effective_limit = request.limit
        if token_budget is not None and effective_limit < 50:
            effective_limit = 50

        use_reranker = self.reranker is not None and request.rerank
        # Cap reranker input: cross-encoder cost is O(n) per candidate, so keep
        # the pool small.  effective_limit * 2 gives enough headroom for diversity
        # without 200+ ONNX forward passes.
        rerank_cap = min(effective_limit * 2, 75)
        candidate_depth = rerank_cap if use_reranker else effective_limit

        # 3b. NLP Temporal Extraction (upstream of RRF)
        # Only extract if no explicit date filters were provided and feature is enabled.
        t0 = _t()
        filters = dict(request.filters) if request.filters else {}
        if (
            self.retrieval_config.temporal_extraction_enabled
            and 'start_date' not in filters
            and 'end_date' not in filters
        ):
            temporal_range = extract_temporal_constraint(
                request.query, reference_date=request.reference_date
            )
            # LLM fallback: if regex found nothing but query sounds temporal
            if (
                temporal_range is None
                and self.concretizer is not None
                and has_ambiguous_temporal_expression(request.query)
            ):
                temporal_range = await self.concretizer.concretize(
                    request.query, reference_date=request.reference_date
                )
                if temporal_range is not None:
                    logger.debug(
                        'Temporal concretization (LLM): %s -> %s to %s',
                        request.query,
                        temporal_range[0],
                        temporal_range[1],
                    )
            if temporal_range is not None:
                filters['start_date'] = temporal_range[0]
                filters['end_date'] = temporal_range[1]
                _nlp_temporal_applied = True
                logger.debug(
                    'Temporal extraction: %s -> %s to %s',
                    request.query,
                    temporal_range[0],
                    temporal_range[1],
                )
            else:
                _nlp_temporal_applied = False
        else:
            _nlp_temporal_applied = False
        t_temporal = _t() - t0

        # 4. Perform Retrieval (Fused across all queries)
        if request.vault_ids:
            filters['vault_ids'] = request.vault_ids

        # Explicitly pass include_stale flag to strategies
        filters['include_stale'] = request.include_stale

        # Pass include_deprioritized flag to strategies (default: exclude)
        filters['include_deprioritized'] = request.include_deprioritized

        # Thread source_context filter for context-scoped retrieval
        if request.source_context:
            filters['source_context'] = request.source_context

        # Thread intent_class / risk_class filters (write-time classifier)
        if request.intent_class is not None:
            filters['intent_class'] = request.intent_class
        if request.risk_class is not None:
            filters['risk_class'] = request.risk_class

        # Pre-compute NER entities off the event loop so graph strategies don't block
        t0 = _t()
        if self.ner_model is not None:
            try:
                # NER cap: one model, one capacity budget. Thread keeps running
                # on timeout; cap prevents thread accumulation.
                async with get_ner_semaphore(), _instrument('ner'):
                    filters['_ner_entities'] = await asyncio.wait_for(
                        asyncio.to_thread(self.ner_model.predict, request.query),
                        timeout=get_ner_call_timeout(),
                    )
            except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as e:
                logger.warning('NER pre-extraction failed: %s', e)
        t_ner = _t() - t0

        # Thread temporal filters for strategy-level date filtering
        if request.after:
            filters['start_date'] = request.after
        if request.before:
            filters['end_date'] = request.before

        # Thread as_of for entity graph temporal validity filtering
        if request.as_of:
            filters['as_of'] = request.as_of

        debug_ctx: DebugContext | None = DebugContext() if request.debug else None

        use_partitioned = self.retrieval_config.fact_type_partitioned_rrf

        # Convert embeddings to plain lists once; used for RRF and possible temporal fallback
        all_embeddings_list = [e.tolist() for e in all_embeddings]
        del all_embeddings  # Free numpy arrays early

        t0 = _t()
        all_ranked_items = []
        for q, q_emb, q_weight in zip(queries, all_embeddings_list, query_weights):
            if use_partitioned:
                items = await self._perform_partitioned_rrf(
                    session,
                    q,
                    q_emb,
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
                    q_emb,
                    candidate_depth,
                    filters,
                    strategies=request.strategies,
                    strategy_weights=request.strategy_weights,
                    debug_ctx=debug_ctx,
                )
            # Weighted candidates for multi-query fusion
            all_ranked_items.append((items, q_weight))
        t_rrf = _t() - t0

        if not all_ranked_items:
            return ([], None)

        # 5. Multi-Query RRF Fusion (Final Blend)
        fused_items = self._fuse_multi_query_results(all_ranked_items, candidate_depth)

        # 5b. Zero-result fallback: if NLP temporal filter produced no results,
        # retry without the temporal constraint so relevance-ranked results come through.
        if not fused_items and _nlp_temporal_applied:
            logger.info(
                'Temporal filter produced zero results — retrying without temporal constraint.'
            )
            filters_relaxed = {
                k: v for k, v in filters.items() if k not in ('start_date', 'end_date')
            }
            all_ranked_items = []
            for q, q_emb, q_weight in zip(queries, all_embeddings_list, query_weights):
                if use_partitioned:
                    items = await self._perform_partitioned_rrf(
                        session,
                        q,
                        q_emb,
                        candidate_depth,
                        filters_relaxed,
                        strategies=request.strategies,
                        strategy_weights=request.strategy_weights,
                        debug_ctx=debug_ctx,
                    )
                else:
                    items = await self._perform_rrf_retrieval(
                        session,
                        q,
                        q_emb,
                        candidate_depth,
                        filters_relaxed,
                        strategies=request.strategies,
                        strategy_weights=request.strategy_weights,
                        debug_ctx=debug_ctx,
                    )
                all_ranked_items.append((items, q_weight))
            fused_items = self._fuse_multi_query_results(all_ranked_items, candidate_depth)

        # Free embedding lists — no longer needed after RRF + fallback
        del all_embeddings_list

        if not fused_items:
            return ([], None)

        # 6. Hydrate Objects (pre-filter applies here when enabled).
        t0 = _t()
        final_results = await self._hydrate_results(
            session, fused_items, apply_pre_filter=request.apply_pre_filter
        )
        t_hydrate = _t() - t0

        # 6b. Filter superseded units
        if not request.include_superseded:
            threshold = self.retrieval_config.superseded_threshold
            final_results = [
                u for u in final_results if extract_confidence_and_count(u)[0] >= threshold
            ]

        # Snapshot AFTER superseded filter so exploration injection cannot
        # surface superseded-but-ACTIVE units.
        hydrated_candidates = list(final_results)

        # 7. Rerank (cap input to avoid O(n) cross-encoder blowup)
        t0 = _t()
        if use_reranker:
            resolved_mw_mode = MWMode.STATIONARY
            if session is not None and primary_vault_id is not None:
                vault_row = await session.get(Vault, primary_vault_id)
                if vault_row is not None:
                    resolved_mw_mode = vault_row.mw_mode
            final_results = await self._rerank_results(
                request.query,
                final_results[:rerank_cap],
                min_score=request.min_score,
                mw_mode=resolved_mw_mode,
            )
        t_rerank = _t() - t0

        # 8. Position-Aware Blending
        if request.fusion_strategy == 'position_aware' and use_reranker:
            final_results = self._apply_position_aware_blending(final_results)

        # 9. Attach Citations
        final_results = self._attach_citations(final_results)

        # 9b. MMR diversity filtering
        t0 = _t()
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
        t_mmr = _t() - t0

        # 9b. Exploration floor: inject under-explored units back into the
        # result set so Memory Worth doesn't become monotonic. Two
        # algorithms are dispatched on ``RetrievalConfig.exploration_mode``:
        #
        # - ``'epsilon_greedy'`` (ship default): outer roll at ε, then
        #   inject up to ``exploration_max_injections`` from the
        #   low-Memory-Worth tail (the existing behaviour).
        # - ``'thompson'``: draw θ ~ Beta(success+1, failure+1) per
        #   eligible candidate, inject the top-θ units (cold-start-fair
        #   by construction; the ε roll is bypassed).
        # - ``'off'``: skip the injector entirely.
        #
        # Both injecting modes share the bypass-pool re-hydration: when
        # the pre-filter is active, the exploration path must see units
        # the main path filtered out, otherwise the very units we want
        # to re-surface are invisible to the injector. Cost profile is
        # mode-asymmetric: ε-greedy pays the bypass round-trip only on
        # the ~ε fraction of calls where the outer roll succeeds (≈5% at
        # the ship default). Thompson pays it on every retrieval, by
        # design — the algorithm samples each call, and degeneracy
        # mitigation lives in the sampler (cold-start fair-shake +
        # MW EMA decay), not in gating. Operators trading away the
        # per-call cost should run ``exploration_mode='off'``.
        exploration_mode = self.retrieval_config.exploration_mode
        if final_results and exploration_mode != 'off':
            from memex_core.memory.retrieval.exploration import (
                inject_exploration_units,
                inject_thompson_exploration,
            )
            from memex_core.metrics import (
                EXPLORATION_INJECTED_TOTAL,
                EXPLORATION_THOMPSON_THETA_DISTRIBUTION,
            )

            if exploration_mode == 'epsilon_greedy':
                should_inject = self._rng.random() < self.retrieval_config.exploration_epsilon
            else:  # thompson
                should_inject = True

            if should_inject:
                exploration_pool = hydrated_candidates
                if request.apply_pre_filter:
                    # Re-hydrate ALL fused items without the pre-filter
                    # predicate so exploration sees units the main path filtered out.
                    bypass_pool = await self._hydrate_results(
                        session, fused_items, apply_pre_filter=False
                    )
                    if not request.include_superseded:
                        threshold = self.retrieval_config.superseded_threshold
                        # Use the SSOT helper for the confidence read so the
                        # falsy-zero handling and ``None`` fallback are
                        # consistent with the rest of
                        # the rerank path (engine.py:1507) and lint
                        # (services/lint_confidence.py).
                        bypass_pool = [
                            u
                            for u in bypass_pool
                            if extract_confidence_and_count(u)[0] >= threshold
                        ]
                    exploration_pool = bypass_pool

                if exploration_pool:
                    pre_inject_count = len(final_results)
                    if exploration_mode == 'epsilon_greedy':
                        # Force inner ε=1.0: we already rolled the dice; the
                        # inner roll would otherwise re-roll and could veto.
                        final_results = inject_exploration_units(
                            final_results,
                            exploration_pool,
                            epsilon=1.0,
                            max_injections=self.retrieval_config.exploration_max_injections,
                            low_mw_threshold=self.retrieval_config.exploration_low_mw_threshold,
                        )
                        injected = len(final_results) - pre_inject_count
                        if injected > 0:
                            EXPLORATION_INJECTED_TOTAL.labels(mode='epsilon_greedy').inc(injected)
                    else:  # thompson
                        final_results, thetas = inject_thompson_exploration(
                            final_results,
                            exploration_pool,
                            max_injections=self.retrieval_config.exploration_max_injections,
                            rng=self._rng,
                        )
                        injected = len(final_results) - pre_inject_count
                        if injected > 0:
                            EXPLORATION_INJECTED_TOTAL.labels(mode='thompson').inc(injected)
                            for theta in thetas:
                                EXPLORATION_THOMPSON_THETA_DISTRIBUTION.observe(theta)

        # 10. Collect resonance update info (deferred to background)
        t0 = _t()
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
        t_resonance = _t() - t0

        logger.warning(
            'PROFILE retrieve | expand=%.0fms embed=%.0fms temporal=%.0fms ner=%.0fms '
            'rrf=%.0fms hydrate=%.0fms rerank=%.0fms mmr=%.0fms resonance=%.0fms '
            'total=%.0fms | queries=%d results=%d',
            t_expand * 1000,
            t_embed * 1000,
            t_temporal * 1000,
            t_ner * 1000,
            t_rrf * 1000,
            t_hydrate * 1000,
            t_rerank * 1000,
            t_mmr * 1000,
            t_resonance * 1000,
            (
                t_expand
                + t_embed
                + t_temporal
                + t_ner
                + t_rrf
                + t_hydrate
                + t_rerank
                + t_mmr
                + t_resonance
            )
            * 1000,
            len(queries),
            len(final_results),
        )

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
        self,
        session: AsyncSession,
        ranked_items: Sequence[Any],
        *,
        apply_pre_filter: bool = True,
    ) -> list[MemoryUnit]:
        """Fetches actual objects from DB and converts them to MemoryUnits.

        When ``apply_pre_filter`` is True, the unit hydration query gains a
        ``WHERE NOT (...)`` predicate that drops obviously-failed (low-Memory Worth)
        and (when ``fsfm_branch_enabled`` is True) decayed candidates before
        they reach the cross-encoder.

        Emits ``HYDRATION_QUERY_DURATION_SECONDS`` and
        ``PRE_FILTER_CANDIDATES_PRUNED``. Metrics emit when ``unit_ids`` is
        non-empty. Empty-input retrievals (model-only results) skip the
        hydration query entirely and so don't emit pre-filter metrics.
        """
        from memex_core.metrics import (
            HYDRATION_QUERY_DURATION_SECONDS,
            PRE_FILTER_CANDIDATES_PRUNED,
        )

        # Dedupe IDs (RRF fusion can produce duplicates across strategies);
        # SQL ``IN`` already deduplicates the fetched set, so an undeduped
        # ``unit_ids`` would inflate the ``pruned`` count below. Use
        # ``dict.fromkeys`` to preserve insertion order.
        unit_ids = list(dict.fromkeys(row.id for row in ranked_items if row.type == 'unit'))
        model_ids = list(dict.fromkeys(row.id for row in ranked_items if row.type == 'model'))

        fetched_units = {}
        fetched_models = {}

        if unit_ids:
            stmt = (
                select(MemoryUnit)
                .where(col(MemoryUnit.id).in_(unit_ids))
                .options(defer(MemoryUnit.embedding))  # type: ignore
                .options(selectinload(MemoryUnit.note))
                .options(selectinload(MemoryUnit.unit_entities))
            )

            # Python-level conditional pre-filter. The Memory Worth branch is always
            # included; the FSFM branch is included only when the config
            # flag is True. SQL-side runtime flags would still reference
            # the missing columns at parse time. The builder owns the
            # single ``text(...)`` boundary — see its docstring for the
            # SECURITY INVARIANT pinning constants-only interpolation.
            pre_filter_clause = _build_pre_filter_clause(
                apply_pre_filter=apply_pre_filter,
                fsfm_branch_enabled=self.retrieval_config.fsfm_branch_enabled,
            )
            if pre_filter_clause is not None:
                stmt = stmt.where(pre_filter_clause)

            t0 = time.monotonic()
            units = (await session.exec(stmt)).all()
            HYDRATION_QUERY_DURATION_SECONDS.observe(time.monotonic() - t0)

            fetched_units = {u.id: u for u in units}

            # Pruned-candidates histogram (always emits, even when the
            # predicate is inactive — value is 0 in that case).
            pruned = max(0, len(unit_ids) - len(fetched_units))
            PRE_FILTER_CANDIDATES_PRUNED.observe(pruned)

            # Calibration signal: emit raw confidence values regardless of
            # confidence_alpha so the pre-flip distribution accumulates.
            for u in units:
                CONFIDENCE_SCORE_DISTRIBUTION.observe(_get_confidence(u))

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
        low_conf_ids = [
            u.id for u in fetched_units.values() if extract_confidence_and_count(u)[0] < 1.0
        ]
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

        # Fetch links for all hydrated units (gated by relation config)
        all_unit_ids = list(fetched_units.keys())
        if all_unit_ids and self.retrieval_config.relations.top_k_related > 0:
            from memex_core.memory.retrieval.note_relations import fetch_memory_links

            links_map = await fetch_memory_links(
                session, all_unit_ids, link_types=['contradicts', 'weakens']
            )
            for uid, links_list in links_map.items():
                if uid in fetched_units:
                    fetched_units[uid].unit_metadata['links'] = [
                        link.model_dump(mode='json') for link in links_list
                    ]

        final_results = []
        for row in ranked_items:
            if row.type == 'unit' and row.id in fetched_units:
                final_results.append(fetched_units[row.id])
            elif row.type == 'model' and row.id in fetched_models:
                final_results.extend(self._convert_mm_to_units(fetched_models[row.id]))

        return final_results

    async def _reranker_score_uncached(self, query: str, texts: list[str]) -> list[float]:
        """Direct cross-encoder call with the standard semaphore + timeout.

        Shared reranker cap across both reranker sites — one model, one
        capacity budget. wait_for cancels the coroutine but the underlying
        thread keeps running.
        """
        if self.reranker is None:
            raise RuntimeError('reranker required for _reranker_score_uncached')
        reranker = self.reranker
        async with get_reranker_semaphore(), _instrument('rerank'):
            raw = await asyncio.wait_for(
                asyncio.to_thread(reranker.score, query, texts),
                timeout=get_reranker_call_timeout(),
            )
        return [float(s) for s in raw]

    async def _reranker_score(
        self,
        query: str,
        results: list[MemoryUnit],
        formatted_texts: list[str],
    ) -> list[float]:
        """Score *results* against *query*, serving cache hits where possible.

        When the cache is disabled the call falls through to the cross-encoder
        directly. Cache key triple is ``(model_version, query_hash, unit_id)``;
        ``unit_id`` is globally unique per Memex schema so the cache cannot
        leak across vaults.
        """
        if self._rerank_cache is None or self.reranker is None:
            return await self._reranker_score_uncached(query, formatted_texts)

        # Defensive fallback for out-of-tree rerankers that don't define
        # ``model_version`` (the protocol now ships a default but duck-typed
        # implementations may not subclass it). With a constant
        # version the cache still works; model upgrades will be invalidated
        # by the TTL backstop rather than structurally.
        model_version = getattr(self.reranker, 'model_version', 'unknown')
        query_h = hash_query(query)
        keys = [(model_version, query_h, unit.id) for unit in results]

        async def _compute(missing_indices: Sequence[int]) -> Sequence[float]:
            sub_texts = [formatted_texts[i] for i in missing_indices]
            return await self._reranker_score_uncached(query, sub_texts)

        return await self._rerank_cache.get_or_compute_batch(keys, _compute)

    async def _rerank_results(
        self,
        query: str,
        results: list[MemoryUnit],
        min_score: float | None = None,
        *,
        mw_mode: MWMode = MWMode.STATIONARY,
    ) -> list[MemoryUnit]:
        """Re-rank results using a cross-encoder with log-additive bounded boosts.

        Applies sigmoid-normalized cross-encoder scores, then composes five
        boost factors in log space and clips the sum symmetrically before
        exponentiating — see :func:`_compose_boosts_logspace` for the
        mechanism. The five boost factors are:

        * **recency boost** -- scaled by ``RetrievalConfig.reranking_recency_alpha``
          (linear decay over 365 days)
        * **temporal proximity boost** -- scaled by
          ``RetrievalConfig.reranking_temporal_alpha`` (uses ``unit.temporal_proximity``
          when available)
        * **Memory Worth boost** -- scaled by
          ``RetrievalConfig.reranking_mw_alpha`` (Beta-Bernoulli posterior mean;
          cold-start mw_boost = 1.0, neutral)
        * **Contradiction-derived confidence boost** -- scaled by
          ``RetrievalConfig.confidence_alpha`` (uses ``unit.confidence``;
          cold-start confidence = 1.0 → boost > 1.0 when alpha > 0; default
          alpha = 0.0 ships boost = 1.0 for every unit)
        * **FSFM decay boost** -- scaled by ``RetrievalConfig.decay_alpha``
          (Ebbinghaus × importance; default alpha = 0.0 ships boost = 1.0)

        Set any alpha to 0 to disable that boost (backward compatible). The
        aggregate metadata multiplier is bounded by
        ``RetrievalConfig.composite_boost_log_clip`` (``L``): the post-clip
        multiplier lies in ``[exp(-L), exp(+L)]``. Ship default ``L = math.inf``
        is a no-op (mathematically identical to the prior multiplicative
        product for strictly positive boost inputs); set a finite ``L`` to
        bound the aggregate metadata influence on ``ce_score``.
        """
        if not self.reranker or not results:
            # Emit zero-input observation so the histogram is always
            # populated for the no-rerank case (observability comparisons
            # need the denominator).
            CROSS_ENCODER_INPUT_COUNT_HISTOGRAM.observe(0)
            return results

        try:
            formatted_texts = []
            for unit in results:
                formatted_texts.append(
                    format_for_reranking(
                        text=unit.text,
                        fact_type=unit.fact_type,
                        context=unit.context,
                        occurred_start=unit.occurred_start,
                        occurred_end=unit.occurred_end,
                    )
                )

            # Cross-encoder input count post-filter. Validates that the
            # pre-filter shrinks the reranker working set.
            CROSS_ENCODER_INPUT_COUNT_HISTOGRAM.observe(len(formatted_texts))

            # Score via cache wrapper. Cache misses fall through to
            # _reranker_score_uncached, which holds the shared reranker
            # semaphore and timeout.
            scores = await self._reranker_score(query, results, formatted_texts)

            # Normalize cross-encoder scores to [0, 1] via sigmoid
            normalized_scores = [1.0 / (1.0 + math.exp(-s)) for s in scores]

            # Compose the five boost factors with log-additive bounded
            # clip; see ``_compose_boosts_logspace`` above.
            from memex_core.memory.retrieval.decay import compute_decay_boost
            from memex_core.services.outcomes import compute_mw_boost

            now = datetime.now(timezone.utc)
            recency_alpha = self.retrieval_config.reranking_recency_alpha
            temporal_alpha = self.retrieval_config.reranking_temporal_alpha
            mw_alpha = self.retrieval_config.reranking_mw_alpha
            confidence_alpha = self.retrieval_config.confidence_alpha
            decay_alpha = self.retrieval_config.decay_alpha
            composite_log_clip = self.retrieval_config.composite_boost_log_clip

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

                # Memory Worth boost (additive-marginal composition)
                mw_boost = compute_mw_boost(
                    success_co_count=unit.success_co_count,
                    failure_co_count=unit.failure_co_count,
                    mw_alpha=mw_alpha,
                    mw_mode=mw_mode,
                    last_outcome_at=unit.last_outcome_at,
                    half_life_days=self.retrieval_config.mw_ema_half_life_days,
                    now=now,
                )
                MW_BOOST_OBSERVED.observe(mw_boost)

                # Contradiction-derived confidence boost.
                # confidence_alpha defaults to 0.0 → boost = 1.0 (no behavior change).
                # When certainty_modulation_enabled is True, the boost is
                # additionally multiplied by certainty = 1 - variance/MAX_VARIANCE
                # (closed-form Beta(1, 1) posterior). Cold-start (count=0) →
                # certainty = 0 → boost collapses to neutral (preserves cold-start
                # safety even with the multiplier active).
                # Single extraction so confidence and evidence_count come
                # from the same clamped read.
                confidence, evidence_count = extract_confidence_and_count(unit)
                # Emit the calibration histogram on every rerank pass —
                # including when ``certainty_modulation`` is OFF (the ship
                # default). Operators need the variance distribution BEFORE
                # flipping the flag to make an informed decision; gating the
                # metric on the flag would leave them blind. The single
                # mean_and_variance call serves both the metric and the
                # (conditional) certainty multiplier.
                _, variance = mean_and_variance(confidence, evidence_count)
                CONFIDENCE_VARIANCE_OBSERVED.observe(variance)
                # Boost-factor range: with ``confidence_alpha`` capped at
                # 2.0 by the ``RetrievalConfig.confidence_alpha`` ``le=2.0``
                # field constraint and ``confidence ∈ [0, 1]`` enforced by
                # ``extract_confidence_and_count``, ``confidence_boost``
                # lies in ``[0.0, 2.0]`` (further compressed toward 1.0
                # when ``certainty < 1`` under modulation, and pinned at
                # 1.0 at cold-start with ``certainty = 0``). Downstream
                # score multiplication therefore caps at a 2× lift / 0×
                # pin. The ``max(..., 0.0)`` floor below is the only
                # explicit guard; no separate ceiling is needed because
                # the inputs cannot exceed those bounds.
                if self.retrieval_config.certainty_modulation_enabled:
                    # Reuse the variance from the metric line above instead
                    # of round-tripping ``certainty(confidence, count)``
                    # (which would re-evaluate ``mean_and_variance``).
                    # The closed form lives in ``certainty_from_variance``
                    # so both call sites share one source of truth.
                    certainty_factor = certainty_from_variance(variance)
                    confidence_boost = max(
                        1.0 + confidence_alpha * (confidence - 0.5) * certainty_factor, 0.0
                    )
                else:
                    confidence_boost = max(1.0 + confidence_alpha * (confidence - 0.5), 0.0)
                CONFIDENCE_BOOST_OBSERVED.observe(confidence_boost)

                decay_boost = compute_decay_boost(unit, decay_alpha=decay_alpha, now=now)
                DECAY_BOOST_OBSERVED.observe(decay_boost)

                boosted_scores.append(
                    _compose_boosts_logspace(
                        ce_score,
                        recency=recency_boost,
                        temporal=temporal_boost,
                        mw=mw_boost,
                        confidence=confidence_boost,
                        decay=decay_boost,
                        log_clip=composite_log_clip,
                    )
                )

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
            # Close the observability gap: the early-return path observes 0,
            # so the exception/fallback path must too. Without this, a
            # reranker exception silently skips the histogram and the metric
            # over-represents successful rerank counts.
            CROSS_ENCODER_INPUT_COUNT_HISTOGRAM.observe(0)
            return results

    async def _compute_pairwise_cosine(
        self, session: AsyncSession, unit_ids: list[UUID]
    ) -> dict[tuple[UUID, UUID], float]:
        """Compute pairwise cosine similarity for a set of memory units via SQL.

        Raw cosine similarities are normalized through the anisotropy corrector
        (Z-score → sigmoid) to counteract embedding anisotropy.
        """
        from sqlalchemy import text

        if len(unit_ids) < 2:
            return {}

        stmt = text("""
            WITH reps AS (
                SELECT id, embedding
                FROM memory_units
                WHERE id = ANY(:unit_ids)
                  AND embedding IS NOT NULL
            )
            SELECT a.id AS id_a, b.id AS id_b,
                   1 - (a.embedding <=> b.embedding) AS similarity
            FROM reps a
            CROSS JOIN reps b
            WHERE a.id < b.id
        """)
        conn = await session.connection()
        result = await conn.execute(stmt, {'unit_ids': [str(uid) for uid in unit_ids]})
        matrix: dict[tuple[UUID, UUID], float] = {}
        for row in result:
            raw_sim = float(row.similarity)
            corrected = self._anisotropy.normalize(raw_sim)
            key = (row.id_a, row.id_b)
            matrix[key] = corrected
            matrix[(row.id_b, row.id_a)] = corrected
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

            virtual_id = uuid5(_VIRTUAL_UNIT_NS, f'{model.id}:{title}')
            units.append(
                MemoryUnit(
                    id=virtual_id,
                    text=f'[{model.name}] {title}: {content}',
                    fact_type=FactTypes.OBSERVATION,
                    status=ContentStatus.ACTIVE,
                    event_date=model.last_refreshed,
                    vault_id=model.vault_id,
                    note_id=None,
                    embedding=[],
                    unit_metadata={
                        'observation': True,
                        'virtual': True,
                        'trend': str(trend.value) if hasattr(trend, 'value') else str(trend),
                        'mental_model_id': str(model.id),
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
