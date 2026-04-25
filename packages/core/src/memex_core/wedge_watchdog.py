"""Wedge watchdog: OS-thread monitor that dumps tracebacks on extraction stalls.

Tracks ``_last_progress_at`` **per stage** (refine/scan/summarize/embed/...).
Fires once when ANY stage has ``gauge > 0`` AND that stage has not recorded
progress in ``wedge_watchdog_seconds``. Per-stage tracking catches the
asymmetric-stall mode: a global timestamp would refresh on every scan tick
and never fire even when refine is stuck.

Reads gauges via the prometheus_client registry's public ``collect()`` API.
Runs on a real OS thread, not asyncio, so it fires even when the event loop
is wedged.
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
    """OS-thread wedge watchdog.

    Fires once when ANY stage has ``inflight > 0`` AND that stage has not
    recorded progress in ``stale_threshold_s``. Stages with no prior
    ``record_progress`` call fall back to construction time, so a stage whose
    gauge is ``> 0`` but has never ticked is treated as wedged once the
    threshold elapses past startup.
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
        # Default for an unseen stage is construction time, so a stage whose
        # gauge is >0 but has never ticked will fire once the threshold
        # elapses past startup.
        self._construction_time = clock()
        self._last_progress_at: dict[str, float] = {}
        self._fired = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name='memex-wedge-watchdog',
        )

    def record_progress(self, stage: str) -> None:
        """Record forward progress for ``stage`` — called by :func:`_instrument`
        on stage exit.

        Thread-safe; the lock makes the per-stage timestamp update atomic
        relative to the watchdog thread's read.
        """
        now = self._clock()
        with self._lock:
            self._last_progress_at[stage] = now

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def fired(self) -> bool:
        return self._fired

    def _read_inflight_per_stage(self) -> dict[str, float]:
        """Return ``{stage: value}`` across both metric families.

        Uses the public ``registry.collect()`` rather than ``Gauge._value.get()``.
        A global sum would hide asymmetric stalls, so we keep per-stage values.
        """
        per_stage: dict[str, float] = {}
        for metric in self._registry.collect():
            if metric.name in _INFLIGHT_METRIC_NAMES:
                for sample in metric.samples:
                    stage = sample.labels.get('stage')
                    if stage is not None:
                        per_stage[stage] = per_stage.get(stage, 0.0) + sample.value
        return per_stage

    def check_once(self) -> bool:
        """Run a single trigger check; return True if the dump fired this call.

        For each stage with gauge > 0, compare ``now - last_progress_at`` to
        the threshold; fire if it exceeds. Stages that have never ticked fall
        back to construction time. Exposed for deterministic testing without
        going through the thread loop.
        """
        if self._fired:
            return False
        now = self._clock()
        per_stage_inflight = self._read_inflight_per_stage()
        with self._lock:
            for stage, inflight in per_stage_inflight.items():
                if inflight <= 0:
                    continue
                last = self._last_progress_at.get(stage, self._construction_time)
                idle = now - last
                if idle >= self._stale_threshold_s:
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
        # Catch broad Exception so the daemon thread survives any unexpected
        # error (e.g. registry-shape change in prometheus_client). BaseException
        # is intentionally not caught — KeyboardInterrupt/SystemExit must
        # still terminate.
        while not self._stop.wait(self._check_interval_s):
            try:
                self.check_once()
            except Exception:
                logger.exception('Wedge watchdog check failed; loop continues')


# None until configure_watchdog() is called from server startup; non-server
# callers (CLI, tests) leave it None and the no-op record_progress() helper
# below short-circuits.
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


def record_progress(stage: str) -> None:
    """No-ops when the watchdog is disabled."""
    if _watchdog is not None:
        _watchdog.record_progress(stage)
