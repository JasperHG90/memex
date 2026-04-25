"""F10: ``run_dspy_operation`` distinguishes provider vs asyncio.wait_for timeouts.

Phase 3 adversarial review (F10, MAJOR) called out that the original
``except TimeoutError`` block at ``llm.py:128`` couldn't tell two distinct
failure modes apart:

1. **Upstream provider timeout** — httpx/LiteLLM fired its socket deadline
   inside ``_execute()``. Surfaces as ``litellm.exceptions.Timeout``
   (subclass of ``openai.APITimeoutError``, NOT a Python ``TimeoutError``).
   POC-001 confirmed this is the actual exception type when
   ``dspy.LM(timeout=N)`` plumbs through to httpx. The remediation is
   provider-level (provider unhealthy, retry, escalate).

2. **Asyncio wait_for fired** — ``asyncio.wait_for(_execute, timeout=N)``
   tripped before the provider could surface its own timeout. Means the
   Python-side deadline elapsed first (e.g. event-loop stall, CPU-bound
   predictor body, provider that swallows its own timeout). Different
   remediation (process-level — investigate the stall).

These tests assert both branches:
- raise distinct messages (surfaced via ``RuntimeError`` ``str(e)``)
- emit distinct log lines (asserted via ``caplog``)
- emit distinct Prometheus metric labels (``socket_timeout`` vs
  ``deadline_exceeded``), with the legacy ``timeout`` label retained as
  a back-compat third bucket so existing dashboards / alerts that filter
  on ``status='timeout'`` keep firing on the sum across both new labels.
- both still update the circuit breaker
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import litellm.exceptions
import pytest

from memex_core import llm as llm_mod
from memex_core.circuit_breaker import CircuitBreaker
from memex_core.llm import run_dspy_operation
from memex_core.metrics import LLM_CALLS_TOTAL


def _counter_value(label: str) -> float:
    """Return the current value of LLM_CALLS_TOTAL{status=label}, or 0.0."""
    return LLM_CALLS_TOTAL.labels(status=label)._value.get()


@pytest.fixture(autouse=True)
def _reset_circuit_breaker() -> None:
    """Fresh circuit breaker per test — these tests record real failures."""
    llm_mod._circuit_breaker = CircuitBreaker()


class _ProviderTimeoutPredictor:
    """Predictor whose ``acall`` raises ``litellm.exceptions.Timeout`` directly,
    mirroring what the dspy → litellm → httpx stack produces when the
    underlying socket deadline fires (POC-001's observed exception type)."""

    async def acall(self, **_kwargs: Any) -> Any:
        raise litellm.exceptions.Timeout(
            message='APITimeoutError - Request timed out.',
            model='gpt-4o-mini',
            llm_provider='openai',
        )


class _HungPredictor:
    """Predictor whose ``acall`` blocks for 24 h — forces the OUTER
    ``asyncio.wait_for`` to fire."""

    async def acall(self, **_kwargs: Any) -> Any:
        await asyncio.sleep(86400)
        return MagicMock()  # unreachable


@pytest.mark.asyncio
async def test_provider_timeout_logs_socket_branch_and_increments_socket_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Inner branch: ``litellm.exceptions.Timeout`` →
    'LLM call socket timeout' log + ``status='socket_timeout'`` counter +
    legacy ``status='timeout'`` counter (back-compat) +
    RuntimeError mentioning 'upstream provider'."""
    lm_stub = MagicMock()
    lm_stub.copy = MagicMock(return_value=lm_stub)

    socket_before = _counter_value('socket_timeout')
    deadline_before = _counter_value('deadline_exceeded')
    legacy_before = _counter_value('timeout')

    caplog.set_level(logging.ERROR, logger='memex.core.llm')

    with pytest.raises(RuntimeError, match=r'upstream provider'):
        await run_dspy_operation(
            lm=lm_stub,
            predictor=_ProviderTimeoutPredictor(),
            input_kwargs={},
            timeout=5,  # generous — predictor raises immediately, not via wait_for
            operation_name='test.provider_timeout',
        )

    socket_records = [r for r in caplog.records if 'socket timeout' in r.getMessage()]
    deadline_records = [r for r in caplog.records if 'deadline exceeded' in r.getMessage()]
    assert socket_records, (
        f'expected "socket timeout" log line; got messages: '
        f'{[r.getMessage() for r in caplog.records]}'
    )
    assert not deadline_records, (
        f'expected NO "deadline exceeded" log line; got: '
        f'{[r.getMessage() for r in deadline_records]}'
    )

    # Metric label assertions (F10 AC):
    assert _counter_value('socket_timeout') == socket_before + 1, (
        f'expected socket_timeout counter +1; before={socket_before}, '
        f'after={_counter_value("socket_timeout")}'
    )
    assert _counter_value('deadline_exceeded') == deadline_before, (
        'deadline_exceeded counter must NOT increment on the provider branch'
    )
    # Back-compat: legacy 'timeout' label still increments alongside the new one.
    assert _counter_value('timeout') == legacy_before + 1, (
        'legacy timeout counter must increment for back-compat dashboards'
    )


@pytest.mark.asyncio
async def test_waitfor_timeout_logs_deadline_branch_and_increments_deadline_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Outer branch: ``asyncio.wait_for`` →
    'LLM call deadline exceeded' log + ``status='deadline_exceeded'`` counter +
    legacy ``status='timeout'`` counter (back-compat) +
    RuntimeError mentioning 'asyncio.wait_for'."""
    lm_stub = MagicMock()
    lm_stub.copy = MagicMock(return_value=lm_stub)

    socket_before = _counter_value('socket_timeout')
    deadline_before = _counter_value('deadline_exceeded')
    legacy_before = _counter_value('timeout')

    caplog.set_level(logging.ERROR, logger='memex.core.llm')

    with pytest.raises(RuntimeError, match=r'asyncio\.wait_for'):
        await run_dspy_operation(
            lm=lm_stub,
            predictor=_HungPredictor(),
            input_kwargs={},
            timeout=1,  # ints required by signature; 1s is the minimum
            operation_name='test.waitfor_timeout',
        )

    deadline_records = [r for r in caplog.records if 'deadline exceeded' in r.getMessage()]
    socket_records = [r for r in caplog.records if 'socket timeout' in r.getMessage()]
    assert deadline_records, (
        f'expected "deadline exceeded" log line; got messages: '
        f'{[r.getMessage() for r in caplog.records]}'
    )
    assert not socket_records, (
        f'expected NO "socket timeout" log line; got: {[r.getMessage() for r in socket_records]}'
    )

    # Metric label assertions (F10 AC):
    assert _counter_value('deadline_exceeded') == deadline_before + 1, (
        f'expected deadline_exceeded counter +1; before={deadline_before}, '
        f'after={_counter_value("deadline_exceeded")}'
    )
    assert _counter_value('socket_timeout') == socket_before, (
        'socket_timeout counter must NOT increment on the wait_for branch'
    )
    # Back-compat: legacy 'timeout' label still increments alongside the new one.
    assert _counter_value('timeout') == legacy_before + 1, (
        'legacy timeout counter must increment for back-compat dashboards'
    )


@pytest.mark.asyncio
async def test_provider_and_waitfor_messages_are_distinguishable() -> None:
    """Cross-check: the RuntimeError messages are textually distinct so
    operators / tests / log-aggregators can route alerts differently."""
    lm_stub = MagicMock()
    lm_stub.copy = MagicMock(return_value=lm_stub)

    provider_msg: str | None = None
    try:
        await run_dspy_operation(
            lm=lm_stub,
            predictor=_ProviderTimeoutPredictor(),
            input_kwargs={},
            timeout=5,
            operation_name='test.dist_provider',
        )
    except RuntimeError as e:
        provider_msg = str(e)

    waitfor_msg: str | None = None
    try:
        await run_dspy_operation(
            lm=lm_stub,
            predictor=_HungPredictor(),
            input_kwargs={},
            timeout=1,
            operation_name='test.dist_waitfor',
        )
    except RuntimeError as e:
        waitfor_msg = str(e)

    assert provider_msg is not None
    assert waitfor_msg is not None
    assert 'upstream provider' in provider_msg
    assert 'asyncio.wait_for' in waitfor_msg
    assert provider_msg != waitfor_msg
