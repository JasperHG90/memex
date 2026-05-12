"""Custom Prometheus metrics for Memex application monitoring."""

from typing import Literal

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Ingestion metrics
# ---------------------------------------------------------------------------

INGESTION_TOTAL = Counter(
    'memex_ingestion_total',
    'Total number of note ingestions',
    ['vault_id', 'status'],
)

INGESTION_DURATION_SECONDS = Histogram(
    'memex_ingestion_duration_seconds',
    'Time spent ingesting a note (seconds)',
    ['vault_id'],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# ---------------------------------------------------------------------------
# Retrieval metrics
# ---------------------------------------------------------------------------

RETRIEVAL_DURATION_SECONDS = Histogram(
    'memex_retrieval_duration_seconds',
    'Time spent on memory retrieval (seconds)',
    ['strategy'],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# ---------------------------------------------------------------------------
# Reflection metrics
# ---------------------------------------------------------------------------

REFLECTION_QUEUE_SIZE = Gauge(
    'memex_reflection_queue_size',
    'Number of pending reflection tasks',
)

REFLECTION_CAS_ABANDONS_TOTAL = Counter(
    'memex_reflection_cas_abandons_total',
    'Number of Phase 5 mental-model writes abandoned because a concurrent '
    'refresh advanced the version column between read and CAS UPDATE.',
)

# ---------------------------------------------------------------------------
# LLM metrics
# ---------------------------------------------------------------------------

LLM_CALLS_TOTAL = Counter(
    'memex_llm_calls_total',
    'Total number of LLM API calls',
    ['status'],
)

LLM_CALL_DURATION_SECONDS = Histogram(
    'memex_llm_call_duration_seconds',
    'Duration of individual LLM calls (seconds)',
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# ---------------------------------------------------------------------------
# Circuit breaker metrics
# ---------------------------------------------------------------------------

CIRCUIT_BREAKER_STATE = Gauge(
    'memex_circuit_breaker_state',
    'Current circuit breaker state (0=closed, 1=open, 2=half-open)',
)

CIRCUIT_BREAKER_REJECTIONS_TOTAL = Counter(
    'memex_circuit_breaker_rejections_total',
    'Total number of calls rejected by the circuit breaker',
)

# ---------------------------------------------------------------------------
# Audit-log metrics (cross-cutting; populated by AuditService._persist)
# ---------------------------------------------------------------------------

MEMEX_AUDIT_LOG_TOTAL = Counter(
    'memex_audit_log_total',
    'Audit log entries written, by action.',
    ['action'],
)

# ---------------------------------------------------------------------------
# Claim-type extraction metrics
# ---------------------------------------------------------------------------

CLAIM_TYPED_UNITS_TOTAL = Counter(
    'memex_claim_typed_units_total',
    'Memory units extracted with an explicit claim_type signal.',
    ['claim_type', 'vault_id'],
)

# ---------------------------------------------------------------------------
# Extraction-pipeline in-flight gauges (wedge diagnostics)
# ---------------------------------------------------------------------------

EXTRACTION_INFLIGHT = Gauge(
    'memex_extraction_inflight',
    'Number of extraction LLM calls currently in flight, by stage.',
    ['stage'],  # scan | refine | summarize | block_summarize
)

SYNC_OFFLOAD_INFLIGHT = Gauge(
    'memex_sync_offload_inflight',
    'Number of synchronous-offload model calls currently in flight, by stage.',
    ['stage'],  # rerank | embed | ner
)

# ---------------------------------------------------------------------------
# Note-append metrics (issue #56)
# ---------------------------------------------------------------------------

NOTE_APPEND_TOTAL = Counter(
    'memex_note_append_total',
    'Total calls to the atomic note-append endpoint, by outcome.',
    ['outcome'],  # success | replayed | conflict | not_found | not_appendable | disabled | error
)

NOTE_APPEND_DURATION_SECONDS = Histogram(
    'memex_note_append_duration_seconds',
    'Wall-clock duration of POST /api/v1/notes/append.',
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# Tracks ingestions whose note_key resolves to an existing non-empty note. Lets
# us see how many callers should migrate to memex_append_note. NOT an error
# counter; informational only.
NOTE_ADD_OVERLAPS_EXISTING_TOTAL = Counter(
    'memex_add_note_with_existing_note_key_total',
    'Ingestions that re-used an existing non-empty note (candidate for memex_append_note).',
    # The label is set by the caller path. Today only ingestion.py emits
    # ``surface='ingest_api'``; ``mcp_add_note`` and ``hermes_add_note`` are
    # documented future surfaces (no code currently emits them).
    ['surface'],
)

# ---------------------------------------------------------------------------
# Memory Worth outcome metrics
# ---------------------------------------------------------------------------

OUTCOME_RECORDED_TOTAL = Counter(
    'memex_outcome_recorded_total',
    'Total outcome recordings by vault and outcome type.',
    ['vault_id', 'outcome'],
)

OUTCOME_VERB_TOTAL = Counter(
    'memex_outcome_verb_total',
    'Per-unit verb classifications recorded by record_outcome.',
    ['vault_id', 'verb'],
)

OUTCOME_COVERAGE_RATIO = Histogram(
    'memex_outcome_coverage_ratio',
    'Reported / retrieved coverage ratio per record_outcome call.',
    ['vault_id', 'mode'],
    buckets=(0.0, 0.1, 0.25, 0.5, 0.75, 1.0),
)

MW_SCORE_DISTRIBUTION = Histogram(
    'memex_mw_score',
    'Distribution of Memory Worth scores observed during outcome recording.',
    ['vault_id', 'mode'],
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

MW_BOOST_OBSERVED = Histogram(
    'memex_mw_boost',
    'Memory Worth boost factors applied during reranking. Neutral is 1.0 (cold-start).',
    buckets=(0.70, 0.80, 0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15, 1.20, 1.30),
)

# ---------------------------------------------------------------------------
# Pre-reranker filter observability
# ---------------------------------------------------------------------------
# CROSS_ENCODER_INPUT_COUNT_HISTOGRAM and EXPLORATION_INJECTED_TOTAL emit
# on every retrieval call (regardless of apply_pre_filter) so observability
# comparisons (with/without filter) are always possible.
# HYDRATION_QUERY_DURATION_SECONDS and PRE_FILTER_CANDIDATES_PRUNED skip
# empty-input retrievals (model-only results) — the hydration query itself is
# skipped in that case, so neither histogram observes a value.

HYDRATION_QUERY_DURATION_SECONDS = Histogram(
    'memex_hydration_query_duration_seconds',
    'Duration of the main hydration query (with the pre-filter when active). '
    'p95 is the re-evaluation gate from §3.4.1 — > 5 ms suggests precomputed '
    'columns become justified.',
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

PRE_FILTER_CANDIDATES_PRUNED = Histogram(
    'memex_pre_filter_candidates_pruned',
    'Count of RRF candidates that the pre-reranker filter dropped per query. '
    'Validates the ~30% reclaim assumption empirically. Always emits 0 when '
    'apply_pre_filter is False or when nothing was pruned.',
    buckets=(0, 1, 2, 5, 10, 20, 30, 40, 50, 75, 100),
)

CROSS_ENCODER_INPUT_COUNT_HISTOGRAM = Histogram(
    'memex_cross_encoder_input_count',
    'Number of candidates the cross-encoder reranker actually scored, '
    'post pre-filter. Should drop from ~70 (cap) to ~50 in the typical case '
    'when the pre-filter is active.',
    buckets=(0, 5, 10, 20, 30, 40, 50, 60, 70, 75),
)

EXPLORATION_INJECTED_TOTAL = Counter(
    'memex_exploration_injected_total',
    'Count of candidates surfaced by the exploration-floor injector, labelled by '
    'algorithm (``epsilon_greedy`` floors at fixed ε; ``thompson`` draws θ ~ Beta '
    'per candidate). Validates that the bypass actually fires under each mode.',
    ['mode'],
)

EXPLORATION_THOMPSON_THETA_DISTRIBUTION = Histogram(
    'memex_exploration_thompson_theta_distribution',
    'Distribution of winning θ values for Thompson-sampled exploration injections. '
    'High concentration near 1.0 indicates the high-posterior degeneracy mode: '
    'extreme posteriors collapse the sampler toward "always pick the highest-MW '
    'unit" and the exploration property is lost. Mitigation lives in the MW EMA '
    'mode (memory worth decay), not in this metric.',
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# ---------------------------------------------------------------------------
# Contradiction-derived confidence reranker composition metrics
# ---------------------------------------------------------------------------

CONFIDENCE_SCORE_DISTRIBUTION = Histogram(
    'memex_confidence_score',
    'Distribution of MemoryUnit.confidence values observed at retrieval hydration. '
    'Independent of confidence_alpha — accumulates calibration data even when the '
    'confidence boost is off (default).',
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

CONFIDENCE_BOOST_OBSERVED = Histogram(
    'memex_confidence_boost',
    'Confidence boost factors applied during reranking. Neutral is 1.0 '
    '(cold-start unit OR confidence_alpha=0).',
    buckets=(0.70, 0.80, 0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15, 1.20, 1.30),
)

# ---------------------------------------------------------------------------
# Two-Factor edge confidence — variance over (confidence, evidence_count)
# ---------------------------------------------------------------------------

CONFIDENCE_VARIANCE_OBSERVED = Histogram(
    'memex_confidence_variance',
    'Closed-form Beta(1, 1) posterior variance derived at retrieval '
    'hydration from (confidence, confidence_evidence_count). Bucketed for '
    'the [0, 1/12 = 0.0833] range. Cold-start units (count=0) emit at 1/12. '
    'Calibration metric for the certainty_modulation_enabled flip — observe '
    'distribution before flipping the flag from False (ship default) to True.',
    buckets=(0.0, 0.001, 0.005, 0.01, 0.02, 0.03, 0.05, 0.06, 0.07, 0.08, 1.0 / 12.0),
)

# ---------------------------------------------------------------------------
# FSFM-lite decay boost reranker composition metric
# ---------------------------------------------------------------------------

DECAY_BOOST_OBSERVED = Histogram(
    'memex_decay_boost',
    'Decay boost factors applied during reranking. Neutral is 1.0 '
    '(NULL importance OR NULL last_outcome_at OR decay_alpha=0).',
    buckets=(0.70, 0.80, 0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15, 1.20, 1.30),
)

# ---------------------------------------------------------------------------
# Write-time classifier metrics
# ---------------------------------------------------------------------------
# The standalone classifier was folded into the fact-extraction signature, so
# the per-call success/error counter (CLASSIFIER_CALLS_TOTAL) was dropped —
# extraction errors are tracked under the extraction-engine instrumentation.
# Distribution + blocked counters are still emitted from the engine's
# post-extraction handling (intent + risk distribution) and the safety filter
# (blocked) respectively.

CLASSIFIER_INTENT_DISTRIBUTION = Counter(
    'memex_classifier_intent_total',
    'Distribution of intent classifications attached to extracted facts.',
    ['intent_class'],
)

CLASSIFIER_RISK_DISTRIBUTION = Counter(
    'memex_classifier_risk_total',
    'Distribution of risk classifications attached to extracted facts.',
    ['risk_class'],
)

CLASSIFIER_BLOCKED_TOTAL = Counter(
    'memex_classifier_blocked_total',
    'Facts refused at ingestion because risk_class=safety.',
    ['vault_id'],
)

DTO_ENUM_COERCION_TOTAL = Counter(
    'memex_dto_enum_coercion_total',
    'MemoryUnitDTO ctor coerced an unrecognised string to the enum default. '
    'Indicates DB schema drift; incremented per offending unit.',
    ['field', 'reason'],
)

# ---------------------------------------------------------------------------
# Diagnostics metrics
# ---------------------------------------------------------------------------

DIAGNOSTICS_MANIFOLD_COMPUTE_SECONDS = Histogram(
    'memex_diagnostics_manifold_compute_seconds',
    'Wall-clock duration of UMAP manifold compute (seconds), per vault.',
    ['vault_id'],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

DIAGNOSTICS_CACHE_HITS_TOTAL = Counter(
    'memex_diagnostics_cache_hits_total',
    'Manifold cache hits (cache_key matched live signature) by vault.',
    ['vault_id'],
)

DIAGNOSTICS_CACHE_MISSES_TOTAL = Counter(
    'memex_diagnostics_cache_misses_total',
    'Manifold cache misses (no cache or cache_key drift) by vault.',
    ['vault_id'],
)

# ---------------------------------------------------------------------------
# Lint metrics
# ---------------------------------------------------------------------------

LINT_FINDINGS_TOTAL = Counter(
    'memex_lint_findings_total',
    'Maintenance proposals emitted by lint rules.',
    ['rule_name', 'lint_type', 'vault_id'],
)

LINT_RUN_DURATION_SECONDS = Histogram(
    'memex_lint_run_duration_seconds',
    'Wall-clock duration of a single lint rule execution (seconds).',
    ['rule_name'],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

CONTRADICTION_RESOLUTION_APPLIED_TOTAL = Counter(
    'memex_contradiction_resolution_applied_total',
    'Winner-proposal applies by resolution action (bounded literals).',
    ['action', 'vault_id'],
)

CONTRADICTION_RESOLUTION_REVERSED_TOTAL = Counter(
    'memex_contradiction_resolution_reversed_total',
    'Winner-proposal reversals by vault.',
    ['vault_id'],
)

# ---------------------------------------------------------------------------
# Entity-cluster collapse maintenance
# ---------------------------------------------------------------------------

# Convention-only label aliases. prometheus_client's ``.labels(**kwargs)``
# accepts arbitrary strings — mypy does not narrow against these.
EntityCollapseScanResultLabel = Literal[
    'proposed', 'rejected_cohesion', 'rescan_updated', 'no_candidates', 'concurrent_skipped'
]
EntityCollapseApplyOutcomeLabel = Literal['success', 'failed']

ENTITY_COLLAPSE_SCAN_EMITTED_TOTAL = Counter(
    'memex_entity_collapse_scan_emitted_total',
    'Cluster proposals produced by the entity-cluster collapse scan.',
    ['result'],
)

ENTITY_COLLAPSE_APPLY_TOTAL = Counter(
    'memex_entity_collapse_apply_total',
    'Cluster collapses applied via EntityService.collapse_cluster.',
    ['outcome'],
)

ENTITY_COLLAPSE_APPLY_DURATION_SECONDS = Histogram(
    'memex_entity_collapse_apply_duration_seconds',
    'Wall-clock duration of EntityService.collapse_cluster (seconds).',
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# ---------------------------------------------------------------------------
# Per-entity advisory lock + reconsolidate / consolidate metrics
# ---------------------------------------------------------------------------

ENTITY_LOCK_ACQUIRES_TOTAL = Counter(
    'memex_entity_lock_acquires_total',
    'Per-entity advisory lock acquisition outcomes.',
    ['outcome'],  # acquired | timeout
)

RECONSOLIDATE_TOTAL = Counter(
    'memex_reconsolidate_total',
    'Total memex_memory_reconsolidate invocations by outcome.',
    ['outcome'],  # success | lock_timeout | error
)

CONSOLIDATE_TOTAL = Counter(
    'memex_consolidate_total',
    'Total memex_memory_consolidate invocations by outcome.',
    ['outcome'],  # success | error
    # No dry_run label — dry_run runs increment as success but the candidate
    # count is in the response body (no separate metric).
)

# ---------------------------------------------------------------------------
# Cross-encoder score cache
# ---------------------------------------------------------------------------

CROSS_ENCODER_CACHE_HITS_TOTAL = Counter(
    'memex_cross_encoder_cache_hits_total',
    'Cross-encoder reranker score cache hits. Hit rate = hits / (hits + misses).',
)

CROSS_ENCODER_CACHE_MISSES_TOTAL = Counter(
    'memex_cross_encoder_cache_misses_total',
    'Cross-encoder reranker score cache misses. '
    'A miss triggers a cross-encoder forward pass and a fill.',
)


# ---------------------------------------------------------------------------
# FSFM-inspired deprioritization scorer
# ---------------------------------------------------------------------------

FSFM_SCORER_RUNS_TOTAL = Counter(
    'memex_fsfm_scorer_runs_total',
    'Total FSFM scorer batch runs (auto-deprioritize ticks) by outcome.',
    ['outcome'],  # success | error | disabled | skipped_locked
)

FSFM_AUTO_DEPRIORITIZED_TOTAL = Counter(
    'memex_fsfm_auto_deprioritized_total',
    'Total memory units auto-deprioritized by the FSFM auto-band.',
)

FSFM_AUTO_BAND_SKIPPED_TOTAL = Counter(
    'memex_fsfm_auto_band_skipped_total',
    'Total candidates the FSFM auto-band skipped, by reason.',
    ['reason'],  # below_threshold | escalation_pending | cooldown_active | unit_missing | lock_held
)
