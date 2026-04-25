"""Tests for custom Prometheus metrics definitions."""

import pytest
from prometheus_client import REGISTRY

from memex_core.metrics import (
    CIRCUIT_BREAKER_REJECTIONS_TOTAL,
    CIRCUIT_BREAKER_STATE,
    EXTRACTION_INFLIGHT,
    INGESTION_DURATION_SECONDS,
    INGESTION_TOTAL,
    LLM_CALL_DURATION_SECONDS,
    LLM_CALLS_TOTAL,
    REFLECTION_QUEUE_SIZE,
    RETRIEVAL_DURATION_SECONDS,
    SYNC_OFFLOAD_INFLIGHT,
)


class TestMetricDefinitions:
    """Verify that all custom metrics are registered and have correct types."""

    def test_ingestion_total_is_counter(self) -> None:
        assert INGESTION_TOTAL._type == 'counter'

    def test_ingestion_duration_is_histogram(self) -> None:
        assert INGESTION_DURATION_SECONDS._type == 'histogram'

    def test_retrieval_duration_is_histogram(self) -> None:
        assert RETRIEVAL_DURATION_SECONDS._type == 'histogram'

    def test_reflection_queue_size_is_gauge(self) -> None:
        assert REFLECTION_QUEUE_SIZE._type == 'gauge'

    def test_llm_calls_total_is_counter(self) -> None:
        assert LLM_CALLS_TOTAL._type == 'counter'

    def test_llm_call_duration_is_histogram(self) -> None:
        assert LLM_CALL_DURATION_SECONDS._type == 'histogram'

    def test_circuit_breaker_state_is_gauge(self) -> None:
        assert CIRCUIT_BREAKER_STATE._type == 'gauge'

    def test_circuit_breaker_rejections_is_counter(self) -> None:
        assert CIRCUIT_BREAKER_REJECTIONS_TOTAL._type == 'counter'

    def test_extraction_inflight_is_gauge(self) -> None:
        assert EXTRACTION_INFLIGHT._type == 'gauge'

    def test_sync_offload_inflight_is_gauge(self) -> None:
        assert SYNC_OFFLOAD_INFLIGHT._type == 'gauge'


class TestMetricLabels:
    """Verify metrics have the expected label names."""

    def test_ingestion_total_labels(self) -> None:
        assert INGESTION_TOTAL._labelnames == ('vault_id', 'status')

    def test_retrieval_duration_labels(self) -> None:
        assert RETRIEVAL_DURATION_SECONDS._labelnames == ('strategy',)

    def test_llm_calls_total_labels(self) -> None:
        assert LLM_CALLS_TOTAL._labelnames == ('status',)

    def test_extraction_inflight_labels(self) -> None:
        assert EXTRACTION_INFLIGHT._labelnames == ('stage',)

    def test_sync_offload_inflight_labels(self) -> None:
        assert SYNC_OFFLOAD_INFLIGHT._labelnames == ('stage',)


class TestMetricOperations:
    """Verify metrics can be incremented/observed without errors."""

    def test_counter_increment(self) -> None:
        INGESTION_TOTAL.labels(vault_id='test', status='success').inc()

    def test_histogram_observe(self) -> None:
        INGESTION_DURATION_SECONDS.labels(vault_id='test').observe(1.5)

    def test_gauge_set(self) -> None:
        REFLECTION_QUEUE_SIZE.set(42)

    def test_llm_calls_success(self) -> None:
        LLM_CALLS_TOTAL.labels(status='success').inc()

    def test_llm_calls_error(self) -> None:
        LLM_CALLS_TOTAL.labels(status='error').inc()

    def test_llm_calls_rejected(self) -> None:
        LLM_CALLS_TOTAL.labels(status='rejected').inc()

    def test_circuit_breaker_state_values(self) -> None:
        CIRCUIT_BREAKER_STATE.set(0)  # closed
        CIRCUIT_BREAKER_STATE.set(1)  # open
        CIRCUIT_BREAKER_STATE.set(2)  # half-open


class TestMetricsRegistered:
    """Verify all custom metrics are discoverable in the default registry."""

    @pytest.mark.parametrize(
        'name',
        [
            # Counters are registered without the _total suffix in prometheus_client
            'memex_ingestion',
            'memex_ingestion_duration_seconds',
            'memex_retrieval_duration_seconds',
            'memex_reflection_queue_size',
            'memex_llm_calls',
            'memex_llm_call_duration_seconds',
            'memex_circuit_breaker_state',
            'memex_circuit_breaker_rejections',
            'memex_extraction_inflight',
            'memex_sync_offload_inflight',
        ],
    )
    def test_metric_in_registry(self, name: str) -> None:
        # Collect all metric names from the default registry
        metric_names = set()
        for metric in REGISTRY.collect():
            metric_names.add(metric.name)
        assert name in metric_names, f'{name} not found in Prometheus registry'


class TestExtractionInflightStages:
    """AC-014: extraction inflight gauge supports the four stage labels.

    The watchdog (#22) reads via REGISTRY.collect() — confirm stage labels
    surface as expected sample tuples on the published metric.
    """

    @pytest.mark.parametrize(
        'stage',
        ['scan', 'refine', 'summarize', 'block_summarize'],
    )
    def test_extraction_stage_label_increments(self, stage: str) -> None:
        # Use a fresh child to avoid bleeding state into other tests.
        EXTRACTION_INFLIGHT.labels(stage=stage).inc()
        sample_values = {
            tuple(sample.labels.items()): sample.value
            for metric in REGISTRY.collect()
            if metric.name == 'memex_extraction_inflight'
            for sample in metric.samples
        }
        assert (('stage', stage),) in sample_values
        assert sample_values[(('stage', stage),)] >= 1.0
        EXTRACTION_INFLIGHT.labels(stage=stage).dec()


class TestSyncOffloadInflightStages:
    """AC-015: sync-offload inflight gauge supports the three stage labels."""

    @pytest.mark.parametrize('stage', ['rerank', 'embed', 'ner'])
    def test_sync_offload_stage_label_increments(self, stage: str) -> None:
        SYNC_OFFLOAD_INFLIGHT.labels(stage=stage).inc()
        sample_values = {
            tuple(sample.labels.items()): sample.value
            for metric in REGISTRY.collect()
            if metric.name == 'memex_sync_offload_inflight'
            for sample in metric.samples
        }
        assert (('stage', stage),) in sample_values
        assert sample_values[(('stage', stage),)] >= 1.0
        SYNC_OFFLOAD_INFLIGHT.labels(stage=stage).dec()
