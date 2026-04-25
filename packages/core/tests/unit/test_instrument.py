"""Unit tests for `_instrument` — verifies the contract every gated
extraction/sync-offload section relies on.

The tests drive a synthetic gated section so they exercise the wrapper in
isolation from any real call site. Real-call-site integration (gauge
values during a counter-LM run, log-line schema during a real fan-out)
lives in #24's PR2 test suite.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY

from memex_core.instrument import _instrument, _resolve_gauge


@pytest.fixture(autouse=True)
def _reset_gauges():
    """Module-level prometheus gauges persist across tests.

    Per the testing-specialist's earlier note (and our own watchdog test
    suite), use per-test fresh state by resetting the affected stage
    children.
    """
    from memex_core.metrics import EXTRACTION_INFLIGHT, SYNC_OFFLOAD_INFLIGHT

    yield

    # Decrement to floor — any leak should be visible in the next test.
    for stage in ('scan', 'refine', 'summarize', 'block_summarize'):
        # Read current value via collect() so we don't poke private API.
        for metric in EXTRACTION_INFLIGHT.collect():
            for sample in metric.samples:
                if sample.labels.get('stage') == stage:
                    while sample.value > 0:
                        EXTRACTION_INFLIGHT.labels(stage=stage).dec()
                        sample = next(
                            s
                            for m in EXTRACTION_INFLIGHT.collect()
                            for s in m.samples
                            if s.labels.get('stage') == stage
                        )
                        if sample.value <= 0:
                            break
    for stage in ('rerank', 'embed', 'ner'):
        for metric in SYNC_OFFLOAD_INFLIGHT.collect():
            for sample in metric.samples:
                if sample.labels.get('stage') == stage:
                    while sample.value > 0:
                        SYNC_OFFLOAD_INFLIGHT.labels(stage=stage).dec()
                        break  # collect again next loop


def _read_gauge(name: str, stage: str) -> float:
    for metric in REGISTRY.collect():
        if metric.name == name:
            for sample in metric.samples:
                if sample.labels.get('stage') == stage:
                    return sample.value
    return 0.0


@pytest.mark.asyncio
async def test_instrument_increments_then_decrements_on_normal_exit() -> None:
    """Gauge balance: +1 inside the block, 0 after."""
    before = _read_gauge('memex_extraction_inflight', 'scan')
    async with _instrument('scan'):
        during = _read_gauge('memex_extraction_inflight', 'scan')
    after = _read_gauge('memex_extraction_inflight', 'scan')

    assert during == before + 1
    assert after == before


@pytest.mark.asyncio
async def test_instrument_decrements_on_exception_path() -> None:
    """Exception inside the body must NOT leak the gauge increment."""
    before = _read_gauge('memex_sync_offload_inflight', 'rerank')

    class _BoomError(Exception):
        pass

    with pytest.raises(_BoomError):
        async with _instrument('rerank'):
            raise _BoomError()

    after = _read_gauge('memex_sync_offload_inflight', 'rerank')
    assert after == before


@pytest.mark.asyncio
async def test_instrument_resolves_extraction_stages() -> None:
    """All four extraction stages are accepted and target EXTRACTION_INFLIGHT."""
    for stage in ('scan', 'refine', 'summarize', 'block_summarize'):
        before = _read_gauge('memex_extraction_inflight', stage)
        async with _instrument(stage):
            during = _read_gauge('memex_extraction_inflight', stage)
            assert during == before + 1
        after = _read_gauge('memex_extraction_inflight', stage)
        assert after == before


@pytest.mark.asyncio
async def test_instrument_resolves_sync_offload_stages() -> None:
    """All three sync-offload stages are accepted and target SYNC_OFFLOAD_INFLIGHT."""
    for stage in ('rerank', 'embed', 'ner'):
        before = _read_gauge('memex_sync_offload_inflight', stage)
        async with _instrument(stage):
            during = _read_gauge('memex_sync_offload_inflight', stage)
            assert during == before + 1
        after = _read_gauge('memex_sync_offload_inflight', stage)
        assert after == before


@pytest.mark.asyncio
async def test_instrument_rejects_unknown_stage() -> None:
    """Typo guard — passing an unknown stage raises ValueError before any inc."""
    with pytest.raises(ValueError, match='Unknown _instrument stage'):
        async with _instrument('not_a_stage'):
            pytest.fail('Body should not run for an unknown stage')


@pytest.mark.asyncio
async def test_instrument_calls_watchdog_record_progress() -> None:
    """On exit, the watchdog's record_progress is called once."""
    with patch('memex_core.instrument._record_watchdog_progress') as mock_record:
        async with _instrument('scan'):
            pass
    assert mock_record.call_count == 1


@pytest.mark.asyncio
async def test_instrument_calls_watchdog_record_progress_on_exception() -> None:
    """Watchdog progress is recorded even when the body raises."""
    with patch('memex_core.instrument._record_watchdog_progress') as mock_record:
        with pytest.raises(RuntimeError):
            async with _instrument('refine'):
                raise RuntimeError('synthetic')
    assert mock_record.call_count == 1


@pytest.mark.asyncio
async def test_instrument_emits_stage_complete_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-018 schema: one record per fan-out task with required fields."""
    with caplog.at_level(logging.INFO, logger='memex.core.instrument'):
        async with _instrument('summarize'):
            await asyncio.sleep(0.001)  # ensure non-zero duration_ms

    records = [r for r in caplog.records if r.message == 'stage_complete']
    assert len(records) == 1, f'Expected exactly one stage_complete record, got {len(records)}'

    rec = records[0]
    # Fields are passed via `extra=` so they land as attributes on the LogRecord.
    # Read via getattr so mypy doesn't complain about attributes it can't see
    # on the typed LogRecord parent class.
    assert getattr(rec, 'stage') == 'summarize'
    duration_ms = getattr(rec, 'duration_ms')
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0
    tasks_in_flight_max = getattr(rec, 'tasks_in_flight_max')
    assert isinstance(tasks_in_flight_max, int)
    assert tasks_in_flight_max >= 1


@pytest.mark.asyncio
async def test_instrument_records_max_inflight_after_increment() -> None:
    """The logged `tasks_in_flight_max` reflects post-increment state.

    If three tasks run concurrently, each sees its own sample after its
    own inc — at peak, the three tasks should observe values like 1, 2, 3
    (depending on scheduling). We assert the max observed across them is
    exactly 3 (the number of concurrent tasks).
    """
    captured: list[int] = []

    async def _one_task() -> None:
        # Sample inside the body via the same path as the wrapper's log line.
        async with _instrument('block_summarize'):
            captured.append(int(_read_gauge('memex_extraction_inflight', 'block_summarize')))
            await asyncio.sleep(0.005)

    await asyncio.gather(*[_one_task() for _ in range(3)])

    # All three increments are visible at peak; the highest observed is 3.
    assert max(captured) == 3
    # Every task's body saw at least 1 (its own).
    assert min(captured) >= 1


@pytest.mark.asyncio
async def test_instrument_concurrent_tasks_balance_to_zero() -> None:
    """Stress: 10 concurrent gated tasks all decrement; gauge returns to baseline."""
    before = _read_gauge('memex_extraction_inflight', 'scan')

    async def _one_task() -> None:
        async with _instrument('scan'):
            await asyncio.sleep(0.001)

    await asyncio.gather(*[_one_task() for _ in range(10)])
    after = _read_gauge('memex_extraction_inflight', 'scan')
    assert after == before


@pytest.mark.asyncio
async def test_instrument_does_not_swallow_body_exception() -> None:
    """A wrapper that swallows exceptions silently is worse than no wrapper.

    Confirms the context manager re-raises through `finally:` cleanly.
    """

    class _UniqueError(Exception):
        pass

    with pytest.raises(_UniqueError):
        async with _instrument('embed'):
            raise _UniqueError('bubble up')


@pytest.mark.asyncio
async def test_instrument_unknown_stage_does_not_increment_anything() -> None:
    """Negative test: a rejected stage must not leave a label child registered.

    prometheus_client lazily creates label children — the failure path must
    abort *before* `gauge.labels(stage=...).inc()`.
    """
    before_extraction = _read_gauge('memex_extraction_inflight', 'made_up')
    before_sync_offload = _read_gauge('memex_sync_offload_inflight', 'made_up')

    with pytest.raises(ValueError):
        async with _instrument('made_up'):
            pass

    after_extraction = _read_gauge('memex_extraction_inflight', 'made_up')
    after_sync_offload = _read_gauge('memex_sync_offload_inflight', 'made_up')
    assert after_extraction == before_extraction
    assert after_sync_offload == before_sync_offload


# Smoke test — ensures the contract test suite at least runs cleanly when
# we promote both files to source. Skip if `instrument.py` is the only
# module on PYTHONPATH (the from-import would fail before pytest collects).
def test_instrument_module_resolves_gauges_from_metrics() -> None:
    from memex_core.metrics import EXTRACTION_INFLIGHT, SYNC_OFFLOAD_INFLIGHT

    assert _resolve_gauge('scan') is EXTRACTION_INFLIGHT
    assert _resolve_gauge('refine') is EXTRACTION_INFLIGHT
    assert _resolve_gauge('summarize') is EXTRACTION_INFLIGHT
    assert _resolve_gauge('block_summarize') is EXTRACTION_INFLIGHT
    assert _resolve_gauge('rerank') is SYNC_OFFLOAD_INFLIGHT
    assert _resolve_gauge('embed') is SYNC_OFFLOAD_INFLIGHT
    assert _resolve_gauge('ner') is SYNC_OFFLOAD_INFLIGHT


def _maybe_unused(_: Any) -> None:
    """Silence unused-import linters on the optional `Any`/`logging` imports
    when we promote — they're useful in the final landing-place edits."""
