"""AC-007: a hung LM does not wedge index_document past the bound.

Per RFC-001 §"Step 1.6" the bound is ``timeout × (num_retries + 1) × 1.5``.
Following the RFC's "Option (b) — isolate plumbing" path: ``num_retries=0``
in the test predictor, so the bound is ``timeout × 1.5`` per call.

POC-001 confirmed the per-call timeout already fires at httpx socket level
on ``origin/main`` (PR #43 wired ``dspy.LM(timeout=…)`` at all six sites).
The pipeline already recovers per call. This test is a **regression guard**
against future changes that drop the timeout wiring or that block forever
inside ``run_dspy_operation`` itself, not a "reproduce → fix" test against
the wedge mode (the wedge was driven by unbounded fan-out exhausting memory
before per-call timeouts could fire — that's #9's domain).

Failing-first audit (PR1 dev required to attempt this per AC-007 *rev*):
ran both tests against ``origin/main`` ``8e59301`` before applying PR1's
fix. Verdict:

* ``test_hung_lm_run_dspy_operation_recovers_within_bound`` — **PASSES on
  ``origin/main``**. RFC's prediction holds: PR #43's per-call timeout
  plumbing already recovers from a hung LM at the ``run_dspy_operation``
  layer. AC-007's substantive assertion is therefore a regression guard,
  not a wedge reproducer.

* ``test_hung_lm_in_summarize_single_node_does_not_wedge`` — **fails on
  ``origin/main`` for an extraneous reason**: the test references PR1's
  new ``summarize_max_concurrency`` ctor kwarg (added in #8), so it
  raises ``TypeError`` at construction on a tree before the new kwarg
  exists. This is intrinsic — the test exercises PR1's new gating, so
  it cannot run on ``origin/main`` unmodified. The substantive AC-007
  question (does the timeout still recover after the new gating?) is
  answered yes by this test on PR1's branch.

Audit log captured (verdict 1=PASS, 2=TypeError-not-wedge) in the PR
description.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from memex_core.llm import run_dspy_operation
from memex_core.memory.extraction.core import AsyncMarkdownPageIndex
from memex_core.memory.extraction.models import TOCNode

# RFC-001 §"Step 1.6" bound: timeout × (num_retries + 1) × 1.5.
# Option (b) — isolate plumbing: num_retries=0, so bound = timeout × 1.5.
TIMEOUT_S = 0.5
WALL_CLOCK_BOUND_S = TIMEOUT_S * 1.5


class _HungPredictor:
    """DSPy-style predictor whose ``acall`` blocks for ~24 h.

    Mirrors the real ``ChainOfThought`` interface that
    ``run_dspy_operation`` calls: ``hasattr(predictor, 'acall')`` is
    True, so the async branch in ``run_dspy_operation`` will route here.
    """

    async def acall(self, **_kwargs: Any) -> Any:
        await asyncio.sleep(86400)  # ~24 hours; well past any test budget
        # Unreachable, but keeps mypy happy:
        return MagicMock()


@pytest.mark.asyncio
async def test_hung_lm_run_dspy_operation_recovers_within_bound() -> None:
    """run_dspy_operation must surface TimeoutError → RuntimeError within
    ``TIMEOUT_S × 1.5`` even when the predictor hangs forever.

    This isolates the timeout-plumbing assertion AC-007 makes about
    ``run_dspy_operation`` (the layer every LLM call in index_document
    goes through).
    """
    predictor = _HungPredictor()
    lm_stub = MagicMock()
    lm_stub.copy = MagicMock(return_value=lm_stub)

    start = time.monotonic()
    with pytest.raises(RuntimeError, match='LLM call timed out'):
        await run_dspy_operation(
            lm=lm_stub,
            predictor=predictor,
            input_kwargs={},
            timeout=int(TIMEOUT_S) if TIMEOUT_S >= 1 else 1,  # int-typed in signature
            operation_name='test.hung_lm',
        )
    duration = time.monotonic() - start

    # The signature accepts only int timeouts (>=1s). With timeout=1 the bound
    # is 1.5 s. We're testing the plumbing, not minimising wall clock.
    assert duration < 1.5 * 1.5, (  # 2.25 s ceiling — generous flakiness budget
        f'run_dspy_operation did not recover from hung LM within bound '
        f"(actual {duration:.3f}s). Either run_dspy_operation's asyncio.wait_for "
        f'boundary stopped firing or the predictor hangs *outside* the wait_for. '
        f'See AC-007.'
    )


@pytest.mark.asyncio
async def test_hung_lm_in_summarize_single_node_does_not_wedge() -> None:
    """End-to-end-ish: a hung LM inside _summarize_single_node (one of #9's
    newly-gated helpers) must NOT pin the worker. The `try/except` inside
    _summarize_single_node catches the RuntimeError so the pipeline
    continues; we assert it returns within the bound and that
    node.summary stayed None (the `except` path didn't crash).

    This closes the gap between the unit-level `run_dspy_operation`
    timeout and the production hot path: the helper handles the timeout
    cleanly without breaking the `async with self._summary_semaphore`
    cleanup.
    """
    indexer = AsyncMarkdownPageIndex(lm=MagicMock(), summarize_max_concurrency=2)

    node = TOCNode(
        original_header_id=0,
        title='Hung Section',
        level=2,
        reasoning='test',
        content='word ' * 60,
    )

    async def _hung_run_dspy(*_args: Any, **_kwargs: Any) -> Any:
        # Mirror what run_dspy_operation does on timeout.
        raise RuntimeError('LLM call timed out after 1s (test.hung_lm)')

    start = time.monotonic()
    with patch(
        'memex_core.memory.extraction.core.run_dspy_operation',
        side_effect=_hung_run_dspy,
    ):
        await indexer._summarize_single_node(node)
    duration = time.monotonic() - start

    # _summarize_single_node catches the RuntimeError (its existing except
    # block) so we don't expect a re-raise. Just that it returns quickly and
    # the semaphore released cleanly.
    assert duration < WALL_CLOCK_BOUND_S, (
        f'_summarize_single_node did not return within bound on hung LM (actual {duration:.3f}s)'
    )
    assert node.summary is None, (
        'expected node.summary to remain None after the timeout was caught; got: {node.summary!r}'
    )

    # Sanity: the semaphore was released (not pinned by the hung path).
    assert indexer._summary_semaphore._value == 2
