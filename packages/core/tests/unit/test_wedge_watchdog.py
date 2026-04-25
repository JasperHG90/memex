"""Unit tests for the wedge watchdog.

All tests use an injected clock and the public `check_once()` API so they
run in well under 1 s of wall-clock time — no `time.sleep`, no thread
scheduling assumptions (RFC-001 §"Step 2.3" + AC-016 wall-clock budget).
"""

from __future__ import annotations

import os
import time

import pytest
from prometheus_client import CollectorRegistry, Gauge

from memex_core import wedge_watchdog
from memex_core.wedge_watchdog import _Watchdog, configure_watchdog


@pytest.fixture
def fake_clock():
    """Mutable clock advanced by tests via `tick()`."""

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

        def tick(self, seconds: float) -> None:
            self.now += seconds

    return _Clock()


@pytest.fixture
def isolated_registry() -> CollectorRegistry:
    """Per-test registry so gauges from one test don't bleed into another."""
    return CollectorRegistry()


@pytest.fixture
def gauges(isolated_registry: CollectorRegistry) -> dict[str, Gauge]:
    extraction = Gauge(
        'memex_extraction_inflight',
        'test',
        ['stage'],
        registry=isolated_registry,
    )
    sync_offload = Gauge(
        'memex_sync_offload_inflight',
        'test',
        ['stage'],
        registry=isolated_registry,
    )
    return {'extraction': extraction, 'sync_offload': sync_offload}


@pytest.fixture(autouse=True)
def _reset_module_watchdog():
    """Ensure no thread leaks between tests using the module singleton."""
    yield
    wedge_watchdog.shutdown_watchdog()


def test_watchdog_fires_on_stall(tmp_path, fake_clock, isolated_registry, gauges) -> None:
    """Trigger condition: gauge > 0 AND idle >= threshold → dump fires once."""
    dump_path = str(tmp_path / 'dump.txt')
    gauges['extraction'].labels(stage='refine').inc()  # one in-flight task
    wd = _Watchdog(
        stale_threshold_s=10.0,
        dump_path=dump_path,
        clock=fake_clock,
        registry=isolated_registry,
    )
    fake_clock.tick(15.0)  # exceed threshold without any record_progress

    fired = wd.check_once()

    assert fired is True
    assert wd.fired is True
    assert os.path.exists(dump_path)
    with open(dump_path) as fh:
        contents = fh.read()
    assert 'Thread' in contents or 'File' in contents  # faulthandler output


def test_watchdog_silent_while_progressing(tmp_path, fake_clock, isolated_registry, gauges) -> None:
    """Progress recordings reset the idle timer — no fire even with gauge > 0."""
    dump_path = str(tmp_path / 'dump.txt')
    gauges['extraction'].labels(stage='scan').inc()
    wd = _Watchdog(
        stale_threshold_s=10.0,
        dump_path=dump_path,
        clock=fake_clock,
        registry=isolated_registry,
    )

    # Five intervals, each progressing within the threshold. Watchdog must stay silent.
    for _ in range(5):
        fake_clock.tick(8.0)
        wd.record_progress()
        assert wd.check_once() is False

    assert wd.fired is False
    assert not os.path.exists(dump_path)


def test_watchdog_silent_when_no_in_flight(tmp_path, fake_clock, isolated_registry, gauges) -> None:
    """Idle past threshold but no in-flight stage → no fire (gauge==0 gate)."""
    dump_path = str(tmp_path / 'dump.txt')
    # NOTE: gauges exist but never incremented — total inflight stays 0
    wd = _Watchdog(
        stale_threshold_s=1.0,
        dump_path=dump_path,
        clock=fake_clock,
        registry=isolated_registry,
    )
    fake_clock.tick(60.0)  # massively exceed the threshold

    assert wd.check_once() is False
    assert wd.fired is False
    assert not os.path.exists(dump_path)


def test_watchdog_fires_only_once(tmp_path, fake_clock, isolated_registry, gauges) -> None:
    """Once-and-only-once semantics — operator restart re-arms (AC-016)."""
    dump_path = str(tmp_path / 'dump.txt')
    gauges['sync_offload'].labels(stage='rerank').inc()
    wd = _Watchdog(
        stale_threshold_s=5.0,
        dump_path=dump_path,
        clock=fake_clock,
        registry=isolated_registry,
    )
    fake_clock.tick(10.0)
    assert wd.check_once() is True

    # Subsequent checks must not re-fire even if we tick further.
    fake_clock.tick(100.0)
    assert wd.check_once() is False
    assert wd.fired is True


def test_watchdog_reads_both_metric_families(
    tmp_path, fake_clock, isolated_registry, gauges
) -> None:
    """Inflight is the sum across extraction + sync_offload families."""
    dump_path = str(tmp_path / 'dump.txt')
    # Only sync-offload has in-flight load — must still trigger.
    gauges['sync_offload'].labels(stage='embed').inc()
    wd = _Watchdog(
        stale_threshold_s=5.0,
        dump_path=dump_path,
        clock=fake_clock,
        registry=isolated_registry,
    )
    fake_clock.tick(10.0)

    assert wd.check_once() is True


def test_watchdog_threshold_is_inclusive(tmp_path, fake_clock, isolated_registry, gauges) -> None:
    """`idle >= threshold` boundary check — exactly at the threshold fires."""
    dump_path = str(tmp_path / 'dump.txt')
    gauges['extraction'].labels(stage='block_summarize').inc()
    wd = _Watchdog(
        stale_threshold_s=5.0,
        dump_path=dump_path,
        clock=fake_clock,
        registry=isolated_registry,
    )
    fake_clock.tick(5.0)  # exactly at threshold

    assert wd.check_once() is True


def test_watchdog_does_not_fire_just_below_threshold(
    tmp_path, fake_clock, isolated_registry, gauges
) -> None:
    """Just-below-threshold idle does not fire — guards off-by-one."""
    dump_path = str(tmp_path / 'dump.txt')
    gauges['extraction'].labels(stage='summarize').inc()
    wd = _Watchdog(
        stale_threshold_s=5.0,
        dump_path=dump_path,
        clock=fake_clock,
        registry=isolated_registry,
    )
    fake_clock.tick(4.999)

    assert wd.check_once() is False


def test_record_progress_is_thread_safe(tmp_path, isolated_registry, gauges) -> None:
    """Concurrent record_progress calls must not corrupt the timestamp.

    Smoke test for the lock — runs 100 record_progress invocations from 4
    OS threads against a real monotonic clock, then asserts the watchdog
    is silent (because progress was just recorded).
    """
    import threading

    dump_path = str(tmp_path / 'dump.txt')
    gauges['extraction'].labels(stage='scan').inc()
    wd = _Watchdog(
        stale_threshold_s=60.0,  # long threshold; we are not testing fires
        dump_path=dump_path,
        clock=time.monotonic,
        registry=isolated_registry,
    )

    def hammer() -> None:
        for _ in range(100):
            wd.record_progress()

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wd.check_once() is False  # progress was just recorded; never stale


def test_configure_watchdog_disabled_when_threshold_none(tmp_path) -> None:
    """`stale_threshold_s=None` returns None and starts no thread."""
    handle = configure_watchdog(None, str(tmp_path / 'dump.txt'))
    assert handle is None
    assert wedge_watchdog._watchdog is None


def test_configure_watchdog_starts_thread(tmp_path) -> None:
    """Enabled config yields a started, daemonised OS thread."""
    handle = configure_watchdog(
        stale_threshold_s=60.0,
        dump_path=str(tmp_path / 'dump.txt'),
        check_interval_s=0.05,
    )
    assert handle is not None
    assert handle.is_alive()
    assert handle._thread.daemon is True
    assert handle._thread.name == 'memex-wedge-watchdog'


def test_configure_watchdog_replaces_existing(tmp_path) -> None:
    """Re-configuring stops the prior watchdog and installs a new one."""
    first = configure_watchdog(
        stale_threshold_s=30.0,
        dump_path=str(tmp_path / 'first.txt'),
        check_interval_s=0.05,
    )
    second = configure_watchdog(
        stale_threshold_s=60.0,
        dump_path=str(tmp_path / 'second.txt'),
        check_interval_s=0.05,
    )
    assert first is not second
    # First was asked to stop. Give the daemon thread up to 0.5 s to wind down
    # (the check_interval_s=0.05 means at most one extra cycle).
    first._stop.set()
    first._thread.join(timeout=0.5)
    assert not first.is_alive()
    assert second.is_alive()


def test_module_record_progress_no_op_when_disabled() -> None:
    """`record_progress()` with no configured watchdog is silent."""
    wedge_watchdog.shutdown_watchdog()  # ensure clean state
    # Must not raise even though no watchdog is configured.
    wedge_watchdog.record_progress()
    assert wedge_watchdog._watchdog is None


def test_module_record_progress_calls_through_when_enabled(tmp_path) -> None:
    """Module-level record_progress reaches the singleton."""
    handle = configure_watchdog(
        stale_threshold_s=60.0,
        dump_path=str(tmp_path / 'dump.txt'),
        check_interval_s=0.05,
    )
    assert handle is not None
    before = handle._last_progress_at
    # Sleep an order of magnitude under the threshold so the timestamp must move.
    time.sleep(0.001)
    wedge_watchdog.record_progress()
    after = handle._last_progress_at
    assert after > before


def test_extraction_config_wedge_watchdog_seconds_round_trip() -> None:
    """AC-017: ExtractionConfig.wedge_watchdog_seconds field round-trips through JSON."""
    from memex_common.config import ExtractionConfig

    cfg = ExtractionConfig(wedge_watchdog_seconds=30)
    dumped = cfg.model_dump()
    assert dumped['wedge_watchdog_seconds'] == 30

    # Default is None (off).
    default_cfg = ExtractionConfig()
    assert default_cfg.wedge_watchdog_seconds is None

    # Validation: ge=1 means 0 must reject.
    with pytest.raises(ValueError):
        ExtractionConfig(wedge_watchdog_seconds=0)


def test_configure_from_settings_disabled_when_none(tmp_path) -> None:
    """Server lifespan helper: None threshold returns None and starts no thread."""
    from memex_core.wedge_watchdog import configure_from_settings

    log_file = tmp_path / 'logs' / 'memex.log'
    handle = configure_from_settings(
        wedge_watchdog_seconds=None,
        log_file_path=str(log_file),
    )
    assert handle is None
    # Parent dir is NOT created when watchdog is disabled — no IO side effect.
    assert not log_file.parent.exists()


def test_configure_from_settings_starts_watchdog_when_enabled(tmp_path) -> None:
    """Server lifespan helper: threshold value starts the watchdog and creates the log dir."""
    from memex_core.wedge_watchdog import configure_from_settings

    log_file = tmp_path / 'logs' / 'memex.log'
    handle = configure_from_settings(
        wedge_watchdog_seconds=30,
        log_file_path=str(log_file),
    )
    assert handle is not None
    assert handle.is_alive()
    # Helper ensures the log directory exists so the dump can be written.
    assert log_file.parent.exists()
    # Dump path siblings the log file (not the log itself).
    assert handle._dump_path == str(log_file.with_name('memex-wedge-dump.txt'))
    # Threshold round-trips as float.
    assert handle._stale_threshold_s == 30.0


def test_configure_from_settings_handles_missing_log_dir(tmp_path) -> None:
    """Helper creates the log directory if it doesn't exist (mkdir parents=True)."""
    from memex_core.wedge_watchdog import configure_from_settings

    deep_path = tmp_path / 'a' / 'b' / 'c' / 'memex.log'
    assert not deep_path.parent.exists()

    handle = configure_from_settings(
        wedge_watchdog_seconds=10,
        log_file_path=str(deep_path),
    )
    assert handle is not None
    assert deep_path.parent.is_dir()
