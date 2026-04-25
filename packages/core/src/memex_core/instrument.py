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
    # Snapshot the gauge value *after* our increment so the log line
    # records the in-flight count this task observed at start. We sample
    # via the public collect() API rather than `_value.get()` (private).
    tasks_in_flight_max = _read_gauge_value(gauge, stage)
    try:
        yield
    finally:
        gauge.labels(stage=stage).dec()
        _record_watchdog_progress()
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
    except (RuntimeError, OSError):
        logger.exception('Failed to read gauge value for stage %r', stage)
    return 0
