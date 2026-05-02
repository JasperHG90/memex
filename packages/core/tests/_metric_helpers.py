"""Shared test helpers for reading Prometheus metric values.

Multiple test modules need to read counter/histogram values via the public
prometheus_client collection API. Keep one implementation here so a metric
shape change doesn't fan out across test files.
"""

from __future__ import annotations


def read_counter_total(metric: object) -> float:
    """Read the cumulative ``_total`` sample from a Prometheus Counter.

    Uses the public ``collect()`` API so the helper survives prometheus_client
    internals churn. Returns ``0.0`` when the counter has no ``_total`` sample
    (the same default as a never-incremented counter).
    """
    samples = list(metric.collect()[0].samples)  # type: ignore[attr-defined]
    for s in samples:
        if s.name.endswith('_total'):
            return float(s.value)
    return 0.0
