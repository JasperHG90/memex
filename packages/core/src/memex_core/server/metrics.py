"""Re-export metrics from memex_core.metrics for backward compatibility."""

from memex_core.metrics import (  # noqa: F401
    CIRCUIT_BREAKER_REJECTIONS_TOTAL,
    CIRCUIT_BREAKER_STATE,
    INGESTION_DURATION_SECONDS,
    INGESTION_TOTAL,
    LLM_CALL_DURATION_SECONDS,
    LLM_CALLS_TOTAL,
    REFLECTION_QUEUE_SIZE,
    RETRIEVAL_DURATION_SECONDS,
)
