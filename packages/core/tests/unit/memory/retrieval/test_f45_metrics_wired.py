"""F45 — observability histograms wiring tests.

Pure-Python pinning tests that run without Docker. End-to-end emission
tests live in ``tests/integration/memory/retrieval/test_int_f40_pre_filter.py``.
"""

from __future__ import annotations

from prometheus_client import REGISTRY


F45_METRIC_NAMES = {
    'memex_hydration_query_duration_seconds',
    'memex_pre_filter_candidates_pruned',
    'memex_cross_encoder_input_count',
    'memex_exploration_injected_total',
}


def test_f45_histograms_importable() -> None:
    """All four metrics are importable from ``memex_core.metrics``."""
    from memex_core import metrics

    assert hasattr(metrics, 'HYDRATION_QUERY_DURATION_SECONDS')
    assert hasattr(metrics, 'PRE_FILTER_CANDIDATES_PRUNED')
    assert hasattr(metrics, 'CROSS_ENCODER_INPUT_COUNT_HISTOGRAM')
    assert hasattr(metrics, 'EXPLORATION_INJECTED_TOTAL')


def test_f45_metrics_registered_to_default_registry() -> None:
    """The four F45 metrics are registered with the default Prometheus
    registry (i.e., available on the server's ``/metrics`` endpoint
    without any extra wiring).

    Registration — not sample emission — is the invariant under test.
    ``memex_exploration_injected_total`` is a *labelled* Counter (``mode``);
    prometheus_client emits no samples for it until a label combination is
    first observed, so iterating ``collector.samples`` would spuriously miss
    a correctly-registered metric. The registry's name index does carry it
    from construction. (prometheus_client also strips the redundant trailing
    ``_total`` from the Counter name to form the base collector name and
    re-appends it for the sample series, so the registered series name is
    ``memex_exploration_injected_total`` as intended.)
    """
    # Force the metrics module to import so its collectors register.
    from memex_core import metrics  # noqa: F401

    registered = set(REGISTRY._names_to_collectors)
    missing = {name for name in F45_METRIC_NAMES if name not in registered}
    assert not missing, f'F45 metric(s) not registered: {missing}'


def test_apply_pre_filter_default_is_true_on_request() -> None:
    """The wire-protocol RetrievalRequest defaults ``apply_pre_filter`` to
    True so existing callers see the latency reclaim by default."""
    from memex_common.schemas import RetrievalRequest as WireRequest

    assert WireRequest(query='dummy').apply_pre_filter is True


def test_apply_pre_filter_default_is_true_on_internal_request() -> None:
    """The internal SQLModel RetrievalRequest mirrors the wire-protocol
    default so the F40 predicate is always opt-out, never opt-in."""
    from memex_core.memory.retrieval.models import RetrievalRequest as CoreRequest

    assert CoreRequest(query='dummy').apply_pre_filter is True


def test_fsfm_branch_enabled_by_default_on_config() -> None:
    """RetrievalConfig.fsfm_branch_enabled defaults to True post-F11
    (Wave 16) — the column migration shipped, so the FSFM SQL clause now
    activates by default. Set False to disable the FSFM branch
    independently of the column migration (kill-switch)."""
    from memex_common.config import RetrievalConfig

    assert RetrievalConfig().fsfm_branch_enabled is True
