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

REFLECTION_QUEUE_DEPTH_BY_TASK_TYPE = Gauge(
    'memex_reflection_queue_depth_by_task_type',
    'Pending+processing reflection queue depth, labeled by task_type. '
    'Operators monitor priority-lane starvation by watching the ratio of '
    "task_type='refresh_observation' to task_type='reflect' under sustained "
    'deprio bursts; sustained refresh-dominance is the trigger to flip '
    '``ReflectionConfig.refresh_obs_priority_lane`` to False.',
    ['task_type'],
)

REFLECTION_QUEUE_DEAD_LETTER_AGE_SECONDS = Gauge(
    'memex_reflection_queue_dead_letter_age_seconds',
    'Age (since last_queued_at) of the oldest DEAD_LETTER refresh-observation '
    'row, in seconds. ``complete_reflection`` deletes only ``reflect``-type '
    'rows; DEAD_LETTER refresh siblings persist as a diagnostic trail without '
    'auto-expiry. Non-zero in steady state signals operator action — inspect '
    'and clear via ``retry_dead_letter`` or manual DELETE.',
)

REFLECTION_CAS_ABANDONS_TOTAL = Counter(
    'memex_reflection_cas_abandons_total',
    'Number of Phase 5 mental-model writes abandoned because a concurrent '
    'refresh advanced the version column between read and CAS UPDATE.',
)

# Deprioritize/refresh-observation lifecycle counters.
DEPRIORITIZE_REJECTED_OBSERVATION_UUID_TOTAL = Counter(
    'memex_deprioritize_rejected_observation_uuid_total',
    'Calls to memory_deprioritize where the unit_id resolved to an observation, '
    'not a memory unit; client received HTTP 400 with source_memory_units.',
)
DEPRIORITIZE_OBSERVATION_EMPTY_EVIDENCE_TOTAL = Counter(
    'memex_deprioritize_observation_empty_evidence_total',
    'Calls to memory_deprioritize where the unit_id resolved to an observation '
    'with zero evidence MUs — invariant violation in mental_models JSONB; the '
    'agent still receives the 400 contract but source_memory_units is empty.',
)

REFRESH_OBSERVATION_TASK_ENQUEUED_TOTAL = Counter(
    'memex_refresh_observation_task_enqueued_total',
    'Refresh-observation tasks enqueued by an MU deprio (after dedupe).',
)

REFRESH_OBSERVATION_TASK_COMPLETED_TOTAL = Counter(
    'memex_refresh_observation_task_completed_total',
    'Refresh-observation tasks completed (any outcome: refreshed, dropped, acked).',
)

REFRESH_OBSERVATION_TASK_ZERO_EVIDENCE_TOTAL = Counter(
    'memex_refresh_observation_task_zero_evidence_total',
    'Refresh tasks that dropped the observation because no surviving evidence remained.',
)

REFLECTION_ENQUEUE_SKIPPED_TOTAL = Counter(
    'memex_reflection_enqueue_skipped_total',
    'Reflection enqueues skipped because the vault disables reflection (kind/policy).',
    ['reason'],
)

VAULT_SUMMARY_SKIPPED_TOTAL = Counter(
    'memex_vault_summary_skipped_total',
    'Vault-summary generations skipped because the vault disables summarization.',
    ['reason'],
)

REFRESH_OBSERVATION_TASK_OBS_ALREADY_PRUNED_TOTAL = Counter(
    'memex_refresh_observation_task_obs_already_pruned_total',
    'Refresh tasks idempotently acked because the observation row was already gone by claim time.',
)

REFRESH_OBSERVATION_TASK_ALREADY_ABSORBED_TOTAL = Counter(
    'memex_refresh_observation_task_already_absorbed_total',
    'Post-lock race check: none of the triggering MUs is still cited; acked idempotently.',
)

REFRESH_OBSERVATION_TASK_DROPPED_BY_LLM_TOTAL = Counter(
    'memex_refresh_observation_task_dropped_by_llm_total',
    'LLM returned should_drop=True AND surviving_evidence_count below the retention threshold.',
)

REFRESH_OBSERVATION_EMPTY_CONTENT_COERCED_TOTAL = Counter(
    'memex_refresh_observation_empty_content_coerced_total',
    'LLM returned should_drop=False with blank content/title (validator bypassed); '
    'observation was coerced to drop. Distinguished from honored should_drop so the '
    'rate of validator-bypass coercions is monitorable separately from real drops.',
)

DEPRIORITIZE_BATCH_UNFLUSHED_NO_VAULT_TOTAL = Counter(
    'memex_deprioritize_batch_unflushed_no_vault_total',
    'batch_set_unit_deprioritized called without vault_id (legacy path); MUs were '
    'flipped to deprioritized but observation refresh was skipped because the '
    'vault-scoped JSONB scan cannot run. Observations citing these MUs will only '
    'refresh on the next routine reflection cycle or the reconcile-tick pass.',
)

REFRESH_OBSERVATION_DROP_OVERRIDDEN_TOTAL = Counter(
    'memex_refresh_observation_drop_overridden_total',
    'Guardrail: LLM said should_drop=True but surviving_evidence_count >= retention threshold; override.',
)

REFRESH_OBSERVATION_MERGED_PREDECESSOR_TOTAL = Counter(
    'memex_refresh_observation_merged_predecessor_total',
    'Phase 4 dropped a predecessor UUID during a merge (kept the lowest-index UUID).',
)

PHASE4_PROVENANCE_MALFORMED_TOTAL = Counter(
    'memex_phase4_provenance_malformed_total',
    'Phase 4 ComparePhaseOutput.provenance entry malformed; fresh uuid4 used instead.',
    ['reason'],
)

RESTORE_OBSERVATION_NO_AFFECTED_ENTITIES_TOTAL = Counter(
    'memex_restore_observation_no_affected_entities_total',
    'Restore of an orphan MU (zero unit_entities rows); no priority reflect enqueued.',
)

REFLECTION_QUEUE_PRIORITY_LANE_ENQUEUED_TOTAL = Counter(
    'memex_reflection_queue_priority_lane_enqueued_total',
    'Priority-lane reflect tasks enqueued (e.g. from restore).',
)

REFRESH_OBSERVATION_RECONCILE_REPAIRED_TOTAL = Counter(
    'memex_refresh_observation_reconcile_repaired_total',
    'Reconcile-tick repaired a missing refresh task for a deprioritized MU.',
    ['vault_id'],
)

REFRESH_OBSERVATION_TASK_LATENCY_SECONDS = Histogram(
    'memex_refresh_observation_task_latency_seconds',
    'Processing wall-clock from claim/dispatch to refresh completion (does NOT '
    'include queue wait between enqueue and claim). Compute queue-wait from '
    'reflection_queue.last_queued_at if needed.',
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

COMPOSITE_BOOST_CLIPPED = Histogram(
    'memex_composite_boost_clipped',
    'Post-clip aggregate multiplier applied to ce_score during reranking, equal to '
    'exp(clip(sum(log(b_i)), -L, +L)) where L = composite_boost_log_clip. At default '
    'L = math.inf the value equals the prior multiplicative product of the five boost '
    'factors (clip is a no-op); for finite L the observed value is bounded to '
    '[exp(-L), exp(+L)] by construction, and mass accumulating at the bucket boundaries '
    'containing exp(±L) is the clip-firing signal. Calls where ce_score or any boost '
    'factor is non-finite, or where composite_boost_log_clip is NaN or negative, '
    'short-circuit before the observe call — see '
    'memex_composite_boost_non_finite_guard_triggered_total for those.',
    buckets=(0.1, 0.3, 0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 2.0, 3.0, 5.0, 10.0, 25.0, 50.0),
)

COMPOSITE_BOOST_NON_FINITE_GUARD_TRIGGERED = Counter(
    'memex_composite_boost_non_finite_guard_triggered_total',
    'Count of reranking composition calls where the non-finite guard short-circuited. '
    'Fires when ce_score is non-finite (NaN or ±inf), or any of the five boost factors '
    'is non-finite, or composite_boost_log_clip is NaN, or composite_boost_log_clip is '
    'negative. composite_boost_log_clip = math.inf is the supported ship default and '
    'does NOT trigger the guard. Non-zero rates mean upstream calibration or config '
    'drifted into territory the composer rejects — investigate immediately.',
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

EXPLORATION_INJECTION_DURATION_SECONDS = Histogram(
    'memex_exploration_injection_duration_seconds',
    'Time spent in the post-MMR exploration-injection step (seconds). Labelled by '
    'mode (``epsilon_greedy`` and ``thompson`` for the low-Memory-Worth path; '
    '``edge_exploration`` for the high-variance-edge re-validation path). Covers the '
    'eligibility scan + Beta draw + metadata annotation; intended to validate the '
    '§2.4.2 latency claim that injection is well under 10ms at current pool sizes.',
    ['mode'],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
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

# rule_name (user-supplied free text) and vault_id are deliberately NOT
# labels here — both would mint unbounded series. lint_type × result is a
# closed 5×4 grid; per-vault/per-rule attribution lives in the structured
# submission logs.
LINT_EXTERNAL_PROPOSALS_TOTAL = Counter(
    'memex_lint_external_proposals_total',
    'Externally-submitted lint proposals by outcome.',
    ['lint_type', 'result'],
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
    ['outcome'],  # success | lock_timeout | error | abandoned
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


# ---------------------------------------------------------------------------
# Procedural (V7) plane
# ---------------------------------------------------------------------------
# Five metrics for the case / procedure / strategy plane. The plane is
# small but it has a distinct identity-anchor doctrine (kind, scope,
# verb, context) and a hybrid BM25+vector search with RRF fusion — so
# its observability needs are not the same as the notes / reflection
# plane. Labels are deliberately bounded (kind is a 3-value Literal,
# operation is a 4-value verb) so cardinality stays O(dozens), not O(rows).

PROCEDURAL_OPERATIONS_TOTAL = Counter(
    'memex_procedural_operations_total',
    'Total procedural-plane write operations, by verb, kind, and outcome.',
    ['operation', 'kind', 'outcome'],  # operation: create|update|upsert|deprecate
    # outcome: success | identity_conflict | not_found | error
)

PROCEDURAL_SEARCH_DURATION_SECONDS = Histogram(
    'memex_procedural_search_duration_seconds',
    'Time spent on a procedural-plane search (BM25 + vector + RRF fusion).',
    ['kind', 'streams'],  # streams: bm25_only | vector_only | rrf | pin_only
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

PROCEDURAL_BRIEFING_CARDS_TOTAL = Counter(
    'memex_procedural_briefing_cards_total',
    'Total briefing cards emitted by `procedural_briefing_cards`, by '
    'context-key-count bucket. The bucket is the number of distinct '
    'context_keys in the request, capped at 10 — a 10+ bucket catches '
    'abuse without unbounded label cardinality.',
    ['context_count_bucket'],  # 1 | 2 | 3 | 4 | 5 | 6_to_10 | 10+
)

PROCEDURAL_IDENTITY_CONFLICT_TOTAL = Counter(
    'memex_procedural_identity_conflict_total',
    'Total identity-anchor collisions on the procedural plane, by kind '
    'and the configured conflict mode (reject | upsert). A spike in '
    '`reject` followed by a quiet `upsert` series is the agent learning '
    'to use the upsert route — that is the desired behaviour, not an '
    'alert.',
    ['kind', 'mode'],
)

PROCEDURAL_DERIVATION_QUEUE_SIZE = Gauge(
    'memex_procedural_derivation_queue_size',
    'Pending rows in the procedural-plane derivation queue. The metric '
    'is only emitted when the derivation worker is enabled — operators '
    'leaving it off see no series, which is the correct null state.',
)
