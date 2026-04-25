"""PR2 closing tests — gauge-per-stage during real fan-outs, stage_complete
log lines per fan-out per document, schema round-trip for the watchdog field.

These tests drive the *real* gated sections (post-#21) rather than synthetic
helpers — the synthetic-helper coverage lives in `test_instrument.py`. The
goal here is to confirm the wrapper fires in production code paths, not
just in isolation.

AC-014 (extraction inflight gauge during counter-LM run), AC-015 (sync-offload
inflight gauge during real call path), AC-017 (schema), AC-018 (stage_complete
log line schema).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY

from memex_core.memory.extraction.core import AsyncMarkdownPageIndex
from memex_core.memory.extraction.models import TOCNode


def _read_gauge(name: str, stage: str) -> float:
    for metric in REGISTRY.collect():
        if metric.name == name:
            for sample in metric.samples:
                if sample.labels.get('stage') == stage:
                    return sample.value
    return 0.0


@pytest.mark.asyncio
async def test_extraction_inflight_summarize_gauge_during_real_call(monkeypatch) -> None:
    """AC-014: the `summarize` gauge ticks during a real `_summarize_single_node`
    call, observed mid-call via a side-effecting fake `run_dspy_operation`.

    Drives the post-#21 production path — confirms `_instrument('summarize')`
    is actually wired into the call site, not just present in `instrument.py`.
    """
    from memex_core.memory.extraction import core as core_mod

    indexer = AsyncMarkdownPageIndex(lm=MagicMock(), summarize_max_concurrency=2)
    node = TOCNode(
        original_header_id=0,
        title='Section',
        level=2,
        reasoning='test',
        content='word ' * 60,
    )

    observed: list[float] = []

    async def _fake_run_dspy(*_args: Any, **kwargs: Any) -> Any:
        # We're inside the `_instrument('summarize')` block here. Read the
        # gauge: it should be ≥ 1 (this call's increment).
        observed.append(_read_gauge('memex_extraction_inflight', 'summarize'))
        await asyncio.sleep(0.001)
        pred = MagicMock()
        pred.summary = MagicMock()
        return pred

    monkeypatch.setattr(core_mod, 'run_dspy_operation', _fake_run_dspy)

    before = _read_gauge('memex_extraction_inflight', 'summarize')
    await indexer._summarize_single_node(node)
    after = _read_gauge('memex_extraction_inflight', 'summarize')

    assert observed == [before + 1], (
        f'Expected gauge to read {before + 1} during the call (one in flight), got {observed}.'
    )
    assert after == before, 'Gauge must return to baseline after the call exits.'


@pytest.mark.asyncio
async def test_extraction_inflight_refine_gauge_during_real_call(monkeypatch) -> None:
    """F6 / AC-014: the `refine` gauge ticks during a real
    `_process_single_node_refinement` call.

    The refine wrapper (`_instrument('refine')`) sits inside
    `async with self._refine_semaphore` and unconditionally wraps the
    body — even if the node doesn't hit the deep-dive branch
    (`node_len > max_len and not node.children`), the gauge increments
    and decrements once per refine task.

    F6 (Phase 3): the prior coverage skipped this stage. With #9's
    refine-recursion split, an instrumentation regression that dropped
    `_instrument('refine')` would silently degrade observability — only
    a per-stage production-call test catches it. We patch
    `_process_single_chunk` and `_build_logical_tree` to confirm the
    side-effecting body runs while the gauge is +1.
    """

    indexer = AsyncMarkdownPageIndex(lm=MagicMock(), refine_max_concurrency=2)
    # Construct a node that will trip the deep-dive branch: node_len > max_len
    # AND no children. start/end_index span 6000 chars; max_len=5000 below.
    node = TOCNode(
        original_header_id=0,
        title='RefineTest',
        level=2,
        reasoning='test',
        content='x' * 6000,
        start_index=0,
        end_index=6000,
    )

    observed: list[float] = []

    async def _fake_chunk(*_args: Any, **_kwargs: Any) -> list:
        # Read the refine gauge inside the chunk-scan call (which itself
        # runs inside `_instrument('refine')`). Should be ≥ 1.
        observed.append(_read_gauge('memex_extraction_inflight', 'refine'))
        return []

    monkeypatch.setattr(indexer, '_process_single_chunk', _fake_chunk)

    before = _read_gauge('memex_extraction_inflight', 'refine')
    await indexer._process_single_node_refinement(node, full_text='x' * 6000, max_len=5000)
    after = _read_gauge('memex_extraction_inflight', 'refine')

    assert observed == [before + 1], (
        f'Expected refine gauge to read {before + 1} during the call '
        f'(one in flight), got {observed}. If empty, _instrument(refine) is '
        f'not wrapping the production refine site (#21 regression).'
    )
    assert after == before, 'Refine gauge must return to baseline after the task exits.'


@pytest.mark.asyncio
async def test_extraction_inflight_block_summarize_gauge_during_real_call(monkeypatch) -> None:
    """AC-014: same shape for `block_summarize` — confirms the third
    extraction stage label is wired."""
    from memex_core.memory.extraction import core as core_mod
    from memex_core.memory.extraction.models import PageIndexBlock

    indexer = AsyncMarkdownPageIndex(lm=MagicMock(), summarize_max_concurrency=2)
    block = PageIndexBlock(
        id='b1',
        seq=0,
        token_count=10,
        start_index=0,
        end_index=100,
        titles_included=['T1'],
        content='content body',
    )

    observed: list[float] = []

    async def _fake_run_dspy(*_args: Any, **kwargs: Any) -> Any:
        observed.append(_read_gauge('memex_extraction_inflight', 'block_summarize'))
        pred = MagicMock()
        pred.block_summary = MagicMock()
        return pred

    monkeypatch.setattr(core_mod, 'run_dspy_operation', _fake_run_dspy)

    before = _read_gauge('memex_extraction_inflight', 'block_summarize')
    await indexer._summarize_single_block(block, 'sections text')
    after = _read_gauge('memex_extraction_inflight', 'block_summarize')

    assert observed == [before + 1], (
        f'Expected block_summarize gauge to read {before + 1} mid-call, got {observed}.'
    )
    assert after == before


@pytest.mark.asyncio
async def test_extraction_inflight_scan_gauge_during_real_call(monkeypatch) -> None:
    """AC-014: confirm `scan` is wired at `_process_single_chunk`."""
    from memex_core.memory.extraction import core as core_mod

    indexer = AsyncMarkdownPageIndex(lm=MagicMock(), scan_max_concurrency=2)

    observed: list[float] = []

    async def _fake_run_dspy(*_args: Any, **kwargs: Any) -> Any:
        observed.append(_read_gauge('memex_extraction_inflight', 'scan'))
        pred = MagicMock()
        pred.detected_headers = []
        return pred

    monkeypatch.setattr(core_mod, 'run_dspy_operation', _fake_run_dspy)

    before = _read_gauge('memex_extraction_inflight', 'scan')
    await indexer._process_single_chunk('chunk text', prev_context='', offset=0)
    after = _read_gauge('memex_extraction_inflight', 'scan')

    assert observed == [before + 1]
    assert after == before


@pytest.mark.asyncio
async def test_sync_offload_inflight_embed_gauge_during_real_call(monkeypatch) -> None:
    """AC-015: the `embed` gauge ticks during a real call through the
    document_search.py site (one of the three embed sites)."""
    from memex_core.memory.retrieval import _offload
    from memex_common.config import ServerConfig

    cfg = ServerConfig(embedding_max_concurrency=2, embedding_call_timeout=10)
    _offload.configure_offload_semaphores(cfg)

    observed: list[float] = []

    def _fake_encode(texts: list[str]) -> list[list[float]]:
        observed.append(_read_gauge('memex_sync_offload_inflight', 'embed'))
        return [[0.0] * 4 for _ in texts]

    # Drive through the production wrapper pattern — `async with sem,
    # _instrument('embed')` matches what api.py / document_search.py /
    # engine.py do at their gated sites.
    from memex_core.instrument import _instrument

    before = _read_gauge('memex_sync_offload_inflight', 'embed')
    async with _offload.get_embedding_semaphore(), _instrument('embed'):
        await asyncio.wait_for(
            asyncio.to_thread(_fake_encode, ['q1']),
            timeout=_offload.get_embedding_call_timeout(),
        )
    after = _read_gauge('memex_sync_offload_inflight', 'embed')

    assert observed == [before + 1]
    assert after == before


@pytest.mark.asyncio
async def test_sync_offload_inflight_rerank_gauge_during_real_call(monkeypatch) -> None:
    """AC-015: `rerank` gauge ticks during the production wrapper pattern."""
    from memex_core.memory.retrieval import _offload
    from memex_common.config import ServerConfig

    cfg = ServerConfig(reranker_max_concurrency=2, reranker_call_timeout=10)
    _offload.configure_offload_semaphores(cfg)

    observed: list[float] = []

    def _fake_score(query: str, texts: list[str]) -> list[float]:
        observed.append(_read_gauge('memex_sync_offload_inflight', 'rerank'))
        return [0.0] * len(texts)

    from memex_core.instrument import _instrument

    before = _read_gauge('memex_sync_offload_inflight', 'rerank')
    async with _offload.get_reranker_semaphore(), _instrument('rerank'):
        await asyncio.wait_for(
            asyncio.to_thread(_fake_score, 'q', ['a', 'b']),
            timeout=_offload.get_reranker_call_timeout(),
        )
    after = _read_gauge('memex_sync_offload_inflight', 'rerank')

    assert observed == [before + 1]
    assert after == before


@pytest.mark.asyncio
async def test_sync_offload_inflight_ner_gauge_during_real_call(monkeypatch) -> None:
    """AC-015: `ner` gauge ticks during the production wrapper pattern."""
    from memex_core.memory.retrieval import _offload
    from memex_common.config import ServerConfig

    cfg = ServerConfig(ner_max_concurrency=2, ner_call_timeout=10)
    _offload.configure_offload_semaphores(cfg)

    observed: list[float] = []

    def _fake_predict(text: str) -> list[Any]:
        observed.append(_read_gauge('memex_sync_offload_inflight', 'ner'))
        return []

    from memex_core.instrument import _instrument

    before = _read_gauge('memex_sync_offload_inflight', 'ner')
    async with _offload.get_ner_semaphore(), _instrument('ner'):
        await asyncio.wait_for(
            asyncio.to_thread(_fake_predict, 'query text'),
            timeout=_offload.get_ner_call_timeout(),
        )
    after = _read_gauge('memex_sync_offload_inflight', 'ner')

    assert observed == [before + 1]
    assert after == before


@pytest.mark.asyncio
async def test_stage_complete_log_emitted_per_fanout_task(monkeypatch) -> None:
    """AC-018: a real fan-out (5 concurrent leaf summaries) emits exactly 5
    `stage_complete` records, each with the required schema fields.

    Captures records via a dedicated handler attached directly to the
    `memex.core.instrument` logger — `pytest caplog` is unreliable here
    because `configure_logging` (called by some test fixtures) sets
    `propagate=False` on the `memex` parent logger, severing the path to
    caplog's root handler.
    """
    from memex_core.memory.extraction import core as core_mod

    indexer = AsyncMarkdownPageIndex(lm=MagicMock(), summarize_max_concurrency=5)
    nodes = [
        TOCNode(
            original_header_id=i,
            title=f'Section {i}',
            level=2,
            reasoning='test',
            content='word ' * 60,
        )
        for i in range(5)
    ]

    async def _fake_run_dspy(*_args: Any, **kwargs: Any) -> Any:
        pred = MagicMock()
        pred.summary = MagicMock()
        return pred

    monkeypatch.setattr(core_mod, 'run_dspy_operation', _fake_run_dspy)

    captured_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    instrument_logger = logging.getLogger('memex.core.instrument')
    handler = _Capture(level=logging.INFO)
    prior_level = instrument_logger.level
    instrument_logger.addHandler(handler)
    instrument_logger.setLevel(logging.INFO)
    try:
        await asyncio.gather(*[indexer._summarize_single_node(n) for n in nodes])
    finally:
        instrument_logger.removeHandler(handler)
        instrument_logger.setLevel(prior_level)

    records = [r for r in captured_records if r.getMessage() == 'stage_complete']
    summarize_records = [r for r in records if getattr(r, 'stage', None) == 'summarize']

    assert len(summarize_records) == 5, (
        f'Expected exactly 5 stage_complete records (one per leaf), got '
        f'{len(summarize_records)} (total stage_complete: {len(records)}).'
    )

    # Every record has the AC-018 schema fields.
    for rec in summarize_records:
        assert getattr(rec, 'stage') == 'summarize'
        duration = getattr(rec, 'duration_ms')
        assert isinstance(duration, int)
        assert duration >= 0
        in_flight_max = getattr(rec, 'tasks_in_flight_max')
        assert isinstance(in_flight_max, int)
        assert in_flight_max >= 1


def test_extraction_config_wedge_watchdog_seconds_schema_round_trip() -> None:
    """AC-017: `ExtractionConfig.wedge_watchdog_seconds` schema round-trips
    cleanly through `model_dump`/`model_validate`. Default is None (off);
    accepts positive int; rejects 0/negative via `ge=1`."""
    from memex_common.config import ExtractionConfig

    # Default: None
    cfg_default = ExtractionConfig()
    assert cfg_default.wedge_watchdog_seconds is None

    # Set, then dump + revive
    cfg_set = ExtractionConfig(wedge_watchdog_seconds=120)
    dumped = cfg_set.model_dump()
    assert dumped['wedge_watchdog_seconds'] == 120
    revived = ExtractionConfig.model_validate(dumped)
    assert revived.wedge_watchdog_seconds == 120

    # ge=1 rejects 0
    with pytest.raises(ValueError):
        ExtractionConfig(wedge_watchdog_seconds=0)

    # ge=1 rejects negative
    with pytest.raises(ValueError):
        ExtractionConfig(wedge_watchdog_seconds=-1)
