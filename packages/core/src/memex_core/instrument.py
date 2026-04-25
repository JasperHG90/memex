"""`_instrument` async context manager — wraps every gated extraction /
sync-offload section with gauge inc/dec, watchdog progress signal, and a
structured ``stage_complete`` log record.

Per RFC-001 §"Step 2.2". Wraps every PR1 + PR1.5 gated section to:

1. Increment the per-stage gauge on entry (AC-014, AC-015).
2. Decrement on exit (including the exception path).
3. Record forward progress via the watchdog (AC-016 — the wording is
   "no in-flight stage has *decremented* in `wedge_watchdog_seconds`").
4. Emit a structured `stage_complete` log record per fan-out task with
   fields `stage`, `duration_ms`, `tasks_in_flight_max` (AC-018).

Contract for callers (the gated-section authors in PR1 + PR1.5):

    async with self._scan_semaphore:                  # PR1 / PR1.5 cap
        async with _instrument('scan'):                # PR2 wrapper
            await run_dspy_operation(...)              # the actual call

The wrapper goes *inside* the semaphore acquire (so the gauge reflects
real concurrent in-flight, not blocked-on-semaphore tasks), and wraps
*only* the call that would actually wedge — not the surrounding bookkeeping.

The `stage` argument must be one of the documented per-family stage
labels (validated at runtime to catch typos before they show up as a
metric label nobody alerts on):

    extraction:    'scan' | 'refine' | 'summarize' | 'block_summarize'
    sync_offload:  'rerank' | 'embed' | 'ner'

`tasks_in_flight_max` snapshots the gauge *just after* this task's
increment — i.e. the max in-flight observed at this task's start. Per RFC
§"Step 2.5" this is one record per task per fan-out. Aggregating across
tasks (max over a fan-out) is a `prom` query concern, not a Python
concern.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator

from prometheus_client import Gauge

from memex_core.metrics import EXTRACTION_INFLIGHT, SYNC_OFFLOAD_INFLIGHT
from memex_core.wedge_watchdog import record_progress as _record_watchdog_progress

logger = logging.getLogger('memex.core.instrument')


_EXTRACTION_STAGES = frozenset({'scan', 'refine', 'summarize', 'block_summarize'})
_SYNC_OFFLOAD_STAGES = frozenset({'rerank', 'embed', 'ner'})


def _resolve_gauge(stage: str) -> Gauge:
    """Pick the gauge family from the stage name. Rejects unknown stages.

    Centralising the mapping in the instrument module (vs. asking each call
    site to pass the gauge) keeps the call site a one-liner and the stage
    label set in one place.
    """
    if stage in _EXTRACTION_STAGES:
        return EXTRACTION_INFLIGHT
    if stage in _SYNC_OFFLOAD_STAGES:
        return SYNC_OFFLOAD_INFLIGHT
    raise ValueError(
        f'Unknown _instrument stage {stage!r}. '
        f'Valid extraction stages: {sorted(_EXTRACTION_STAGES)}; '
        f'valid sync_offload stages: {sorted(_SYNC_OFFLOAD_STAGES)}.'
    )


@contextlib.asynccontextmanager
async def _instrument(stage: str) -> AsyncIterator[None]:
    """Per-task instrumentation wrapper for a gated LLM/model call.

    Increments the right gauge on entry, decrements on exit (always — the
    `try/finally` guarantees this for the exception path too), notifies the
    wedge watchdog of forward progress, and emits a `stage_complete` log
    record with timing + max-in-flight at start.

    Designed for one task per fan-out — call sites typically do
    ``await asyncio.gather(*[_one_task(...) for ...])`` where each
    ``_one_task`` body contains an ``async with _instrument(stage):``.
    """
    gauge = _resolve_gauge(stage)
    started = time.monotonic()
    gauge.labels(stage=stage).inc()
    # F12 (Phase 3): the gauge-value snapshot lives inside the try block so
    # ANY exception escaping `_read_gauge_value` runs the finally clause and
    # decrements the gauge. Pre-F12, the snapshot ran above the try and a
    # leak from `_read_gauge_value` (e.g. a prometheus_client internal-state
    # corruption raising KeyError, outside the narrow except) would orphan
    # the .inc() above forever. _read_gauge_value also catches `Exception`
    # internally as defence in depth, so two independent guarantees protect
    # the increment/decrement balance. Pre-initialise tasks_in_flight_max=0
    # so the stage_complete log line in `finally` can still emit even if
    # the snapshot read raised before assigning a real value.
    tasks_in_flight_max = 0
    try:
        tasks_in_flight_max = _read_gauge_value(gauge, stage)
        yield
    finally:
        gauge.labels(stage=stage).dec()
        # Per F3, forward the stage label so the watchdog tracks staleness
        # per-stage rather than against a global timestamp (a global one
        # would mask asymmetric stalls — e.g. scan ticking while refine wedges).
        _record_watchdog_progress(stage)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            'stage_complete',
            extra={
                'stage': stage,
                'duration_ms': elapsed_ms,
                'tasks_in_flight_max': tasks_in_flight_max,
            },
        )


def _read_gauge_value(gauge: Gauge, stage: str) -> int:
    """Read the current value of the (gauge, stage) child via the registry.

    Public protocol read — RFC-001 §A7. Falls back to 0 on any unexpected
    failure (the gauge family must always exist in the global registry; a
    failure here means an upstream bug, not a user-facing error).

    F12 (Phase 3): catches `Exception`, not just `(RuntimeError, OSError)`.
    Earlier versions used the narrow tuple, but a `prometheus_client`
    internal-state corruption (or a future registry-protocol change) could
    raise something outside that tuple. Letting it escape would orphan
    the `gauge.labels(stage).inc()` call in `_instrument` because the
    exception would surface BEFORE the `try/finally` block was entered,
    leaving the increment with no matching decrement. Logging via
    `logger.exception` preserves the underlying bug for debugging while
    keeping the wrapper's "increment + decrement balance" invariant.
    """
    try:
        # Gauge.labels(...) returns a child; the child has a `_value.get()`
        # private accessor, but using it is forbidden by §A7. The supported
        # path is registry-collect; on the per-stage child it's:
        #   any sample with labels {'stage': stage}
        for metric in gauge.collect():
            for sample in metric.samples:
                if sample.labels.get('stage') == stage:
                    return int(sample.value)
    except Exception:
        logger.exception('Failed to read gauge value for stage %r', stage)
    return 0
