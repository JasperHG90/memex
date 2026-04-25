"""Wedge watchdog: OS-thread monitor that dumps tracebacks on extraction stalls.

Fires once when no in-flight extraction stage has decremented in
``wedge_watchdog_seconds`` while at least one stage gauge is ``> 0``.

Reads gauges via the prometheus_client registry's public ``collect()`` API —
no private state, no parallel counters (RFC-001 §A7). Runs on a real OS
thread, not asyncio (RFC-001 §A4) so it fires even when the event loop is
wedged.

The module exposes a singleton :data:`_watchdog` that the gated
``_instrument`` context manager calls :meth:`_Watchdog.record_progress` on
every time a stage decrement happens. Server startup wires the singleton via
:func:`configure_watchdog` based on ``ExtractionConfig.wedge_watchdog_seconds``.
"""

from __future__ import annotations

import faulthandler
import logging
import threading
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING

from prometheus_client import REGISTRY

if TYPE_CHECKING:
    from prometheus_client.registry import CollectorRegistry

logger = logging.getLogger('memex.core.wedge_watchdog')

_INFLIGHT_METRIC_NAMES = frozenset(('memex_extraction_inflight', 'memex_sync_offload_inflight'))


class _Watchdog:
    """OS-thread wedge watchdog (RFC-001 §"Step 2.3").

    Fires once when ``inflight > 0`` AND ``now - last_progress_at >= threshold``.
    Both signals must align before the dump fires — the AC-016 wording is
    "no in-flight stage has decremented in ``wedge_watchdog_seconds``", so
    event-driven progress (via :meth:`record_progress`) plus a public-API
    inflight read are the two halves of the trigger.
    """

    def __init__(
        self,
        stale_threshold_s: float,
        dump_path: str,
        clock: Callable[[], float] = monotonic,
        check_interval_s: float = 0.5,
        registry: 'CollectorRegistry' = REGISTRY,
    ) -> None:
        self._stale_threshold_s = stale_threshold_s
        self._dump_path = dump_path
        self._clock = clock
        self._check_interval_s = check_interval_s
        self._registry = registry
        self._last_progress_at = clock()
        self._fired = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name='memex-wedge-watchdog',
        )

    def record_progress(self) -> None:
        """Record forward progress — called by :func:`_instrument` on stage exit.

        Thread-safe; the lock makes the timestamp update atomic relative to the
        watchdog thread's read.
        """
        now = self._clock()
        with self._lock:
            self._last_progress_at = now

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def fired(self) -> bool:
        return self._fired

    def _read_inflight(self) -> float:
        """Sum the values of all stage gauges across both metric families.

        Uses ``registry.collect()`` (public protocol per RFC-001 §A7) rather
        than ``Gauge._value.get()``. Cost is sub-microsecond at our metric
        count (~10).
        """
        total = 0.0
        for metric in self._registry.collect():
            if metric.name in _INFLIGHT_METRIC_NAMES:
                for sample in metric.samples:
                    total += sample.value
        return total

    def check_once(self) -> bool:
        """Run a single trigger check; return True if the dump fired this call.

        Exposed for deterministic testing — drives one iteration without going
        through the thread loop, so tests can inject a fake clock and assert
        the trigger semantics without wall-clock waits.
        """
        if self._fired:
            return False
        with self._lock:
            idle = self._clock() - self._last_progress_at
        inflight = self._read_inflight()
        if inflight > 0 and idle >= self._stale_threshold_s:
            self._dump()
            self._fired = True
            return True
        return False

    def _dump(self) -> None:
        try:
            with open(self._dump_path, 'w') as fh:
                faulthandler.dump_traceback(file=fh, all_threads=True)
        except OSError as exc:
            logger.error('Wedge watchdog failed to write traceback dump: %s', exc, exc_info=True)

    def _run(self) -> None:
        # Sleep on the stop event so shutdown doesn't have to wait a full
        # check_interval. wait() returns True when set, False on timeout —
        # either way we fall out of the loop or do another check.
        while not self._stop.wait(self._check_interval_s):
            try:
                self.check_once()
            except (RuntimeError, OSError):
                logger.exception('Wedge watchdog check failed')


# Module-level singleton. None until configure_watchdog() is called from
# server startup. The _instrument context manager checks `_watchdog is not
# None` before calling record_progress so non-server usages (CLI, tests
# without server fixtures) don't pay the watchdog cost.
_watchdog: _Watchdog | None = None


def configure_watchdog(
    stale_threshold_s: float | None,
    dump_path: str,
    *,
    clock: Callable[[], float] = monotonic,
    check_interval_s: float = 0.5,
    registry: 'CollectorRegistry' = REGISTRY,
) -> _Watchdog | None:
    """Configure and start the module-level watchdog singleton.

    Pass ``stale_threshold_s=None`` to disable (the default for production
    deployments that have not opted in). Returns the watchdog instance for
    callers that need the handle (e.g. for shutdown), or ``None`` when
    disabled.

    Idempotent for the disabled case. If a previous watchdog is running and
    this is called with new args, the old one is stopped first.
    """
    global _watchdog
    if _watchdog is not None:
        _watchdog.stop()
        _watchdog = None
    if stale_threshold_s is None:
        return None
    _watchdog = _Watchdog(
        stale_threshold_s=stale_threshold_s,
        dump_path=dump_path,
        clock=clock,
        check_interval_s=check_interval_s,
        registry=registry,
    )
    _watchdog.start()
    return _watchdog


def shutdown_watchdog() -> None:
    """Stop the module-level watchdog (if running). Safe to call repeatedly."""
    global _watchdog
    if _watchdog is not None:
        _watchdog.stop()
        _watchdog = None


def configure_from_settings(
    wedge_watchdog_seconds: int | None,
    log_file_path: str,
) -> '_Watchdog | None':
    """Configure the watchdog from server settings — used by lifespan wiring.

    Computes the dump path from the configured log file (siblings the log
    rather than introducing a new config field) and ensures the parent
    directory exists. Returns None when ``wedge_watchdog_seconds`` is None
    (the opt-in default is "off"), matching ``configure_watchdog``'s contract.
    """
    if wedge_watchdog_seconds is None:
        return None
    import pathlib

    log_path = pathlib.Path(log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    dump_path = str(log_path.with_name('memex-wedge-dump.txt'))
    return configure_watchdog(
        stale_threshold_s=float(wedge_watchdog_seconds),
        dump_path=dump_path,
    )


def record_progress() -> None:
    """Module-level helper — used by ``_instrument`` to avoid passing the
    handle through every gated section. No-ops when the watchdog is disabled.
    """
    if _watchdog is not None:
        _watchdog.record_progress()
