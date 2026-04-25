"""`_instrument` async context manager — wraps every gated extraction /
sync-offload section with gauge inc/dec, watchdog progress signal, and a
structured ``stage_complete`` log record.

Contract for callers:

    async with self._scan_semaphore:
        async with _instrument('scan'):
            await run_dspy_operation(...)

The wrapper goes *inside* the semaphore acquire so the gauge reflects real
concurrent in-flight, not blocked-on-semaphore tasks, and wraps only the
call that would actually wedge.

Valid stages:

    extraction:    'scan' | 'refine' | 'summarize' | 'block_summarize'
    sync_offload:  'rerank' | 'embed' | 'ner'
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
    """Pick the gauge family from the stage name. Rejects unknown stages."""
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
    # Snapshot read lives inside the try so any exception still runs finally
    # and decrements the gauge — without that, a snapshot-read leak orphans
    # the .inc() forever.
    tasks_in_flight_max = 0
    try:
        tasks_in_flight_max = _read_gauge_value(gauge, stage)
        yield
    finally:
        gauge.labels(stage=stage).dec()
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

    Catches broad ``Exception`` so a registry-protocol surprise can't escape
    and orphan the matching ``.inc()`` in :func:`_instrument`. Falls back to
    ``0``; the gauge family is always registered, so a read failure is an
    upstream bug, not a user-visible error.
    """
    try:
        for metric in gauge.collect():
            for sample in metric.samples:
                if sample.labels.get('stage') == stage:
                    return int(sample.value)
    except Exception:
        logger.exception('Failed to read gauge value for stage %r', stage)
    return 0
