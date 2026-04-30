"""Custom Prometheus metrics for Memex application monitoring."""

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
# Memory Worth (MW) outcome metrics (F1a)
# ---------------------------------------------------------------------------

OUTCOME_RECORDED_TOTAL = Counter(
    'memex_outcome_recorded_total',
    'Total outcome recordings by vault and outcome type.',
    ['vault_id', 'outcome'],
)

MW_SCORE_DISTRIBUTION = Histogram(
    'memex_mw_score',
    'Distribution of MW scores observed during outcome recording.',
    ['vault_id'],
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

MW_BOOST_OBSERVED = Histogram(
    'memex_mw_boost',
    'MW boost factors applied during reranking. Neutral is 1.0 (cold-start).',
    buckets=(0.70, 0.80, 0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15, 1.20, 1.30),
)

# ---------------------------------------------------------------------------
# Write-time classifier metrics (F25)
# ---------------------------------------------------------------------------

CLASSIFIER_CALLS_TOTAL = Counter(
    'memex_classifier_calls_total',
    'Write-time classifier calls by status (success | error | fallback).',
    ['status'],
)

CLASSIFIER_INTENT_DISTRIBUTION = Counter(
    'memex_classifier_intent_total',
    'Distribution of intent classifications produced by the write-time classifier.',
    ['intent_class'],
)

CLASSIFIER_RISK_DISTRIBUTION = Counter(
    'memex_classifier_risk_total',
    'Distribution of risk classifications produced by the write-time classifier.',
    ['risk_class'],
)

CLASSIFIER_BLOCKED_TOTAL = Counter(
    'memex_classifier_blocked_total',
    'Facts refused at ingestion because the classifier flagged risk_class=safety.',
    ['vault_id'],
)
