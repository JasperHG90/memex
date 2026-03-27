"""Tests that operation_name is propagated to OpenTelemetry spans via run_dspy_operation."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import memex_core.llm as llm_mod
from memex_core.llm import run_dspy_operation

# Guard imports — skip entire module if tracing extras missing
otel_available = True
try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:
    otel_available = False

oi_available = llm_mod._oi_using_attributes is not None

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """Ensure a fresh circuit breaker for each test."""
    from memex_core.circuit_breaker import CircuitBreaker

    llm_mod._circuit_breaker = CircuitBreaker()


@pytest.fixture
def otel_spans():
    """Fixture providing InMemorySpanExporter and a tracer wired to it.

    Sets llm_mod._tracer directly (no global provider override needed).
    Restores original _tracer on teardown.
    """
    if not otel_available:
        pytest.skip('opentelemetry SDK not installed')

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer('memex.llm')

    old_tracer = llm_mod._tracer
    llm_mod._tracer = tracer

    yield exporter

    llm_mod._tracer = old_tracer
    provider.shutdown()


def _make_dummy_lm():
    from dspy.utils.dummies import DummyLM

    return DummyLM([{'response': 'ok'}])


def _make_mock_predictor(return_value=None):
    pred = MagicMock()
    pred.acall = AsyncMock(return_value=return_value or MagicMock())
    return pred


async def test_creates_named_parent_span(otel_spans):
    """run_dspy_operation creates a span named after operation_name."""
    await run_dspy_operation(
        lm=_make_dummy_lm(),
        predictor=_make_mock_predictor(),
        input_kwargs={},
        operation_name='reflection.seed',
    )

    spans = otel_spans.get_finished_spans()
    span_names = [s.name for s in spans]
    assert 'reflection.seed' in span_names


async def test_using_attributes_called_with_metadata():
    """using_attributes is called with correct metadata dict."""
    if not oi_available:
        pytest.skip('openinference not installed')

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_using = MagicMock(return_value=mock_ctx)

    old_using = llm_mod._oi_using_attributes
    llm_mod._oi_using_attributes = mock_using

    try:
        await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=_make_mock_predictor(),
            input_kwargs={},
            operation_name='extraction.facts',
        )

        mock_using.assert_called_once_with(metadata={'memex.stage': 'extraction.facts'})
    finally:
        llm_mod._oi_using_attributes = old_using


async def test_works_without_tracing_deps():
    """run_dspy_operation works when tracing deps are not available."""
    old_tracer = llm_mod._tracer
    old_using = llm_mod._oi_using_attributes

    llm_mod._tracer = None
    llm_mod._oi_using_attributes = None

    try:
        result = await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=_make_mock_predictor(),
            input_kwargs={},
            operation_name='reflection.seed',
        )
        assert result is not None
    finally:
        llm_mod._tracer = old_tracer
        llm_mod._oi_using_attributes = old_using


async def test_default_operation_name():
    """Default operation_name is 'dspy' when not specified."""
    if not oi_available:
        pytest.skip('openinference not installed')

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_using = MagicMock(return_value=mock_ctx)

    old_using = llm_mod._oi_using_attributes
    llm_mod._oi_using_attributes = mock_using

    try:
        await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=_make_mock_predictor(),
            input_kwargs={},
        )

        mock_using.assert_called_once_with(metadata={'memex.stage': 'dspy'})
    finally:
        llm_mod._oi_using_attributes = old_using


async def test_multiple_operation_names(otel_spans):
    """Different operation_name values produce correctly named spans."""
    for op_name in ['extraction.frontmatter', 'reflection.validate', 'contradiction.triage']:
        otel_spans.clear()
        await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=_make_mock_predictor(),
            input_kwargs={},
            operation_name=op_name,
        )
        spans = otel_spans.get_finished_spans()
        assert any(s.name == op_name for s in spans), (
            f'Expected span named {op_name!r}, got {[s.name for s in spans]}'
        )


async def test_mock_dspy_lm_accepts_operation_name(mock_dspy_lm):
    """MockDspyLM._mock_run_dspy accepts the operation_name kwarg via **kwargs."""
    mock_dspy_lm.set_responses([MagicMock()])

    result = await mock_dspy_lm._mock_run_dspy(
        lm=None,
        predictor=None,
        input_kwargs={},
        operation_name='test.op',
    )
    assert mock_dspy_lm.call_count == 1
    assert result is not None


async def test_tracing_does_not_swallow_exceptions():
    """Exceptions from the predictor propagate even when tracing is enabled."""
    pred = MagicMock()
    pred.acall = AsyncMock(side_effect=ValueError('test error'))

    with pytest.raises(ValueError, match='test error'):
        await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=pred,
            input_kwargs={},
            operation_name='reflection.seed',
        )


async def test_metadata_propagates_to_child_span():
    """Integration: metadata from using_attributes appears on spans created inside the context.

    This exercises the exact code path used by LiteLLMInstrumentor:
    it calls get_attributes_from_context() to read context vars set by using_attributes().
    """
    if not otel_available or not oi_available:
        pytest.skip('opentelemetry + openinference required')

    from openinference.instrumentation import get_attributes_from_context, using_attributes

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer('memex.test')

    try:
        # Simulate what run_dspy_operation does (parent span + using_attributes)
        # and what LiteLLMInstrumentor does (child span with get_attributes_from_context)
        with tracer.start_as_current_span('reflection.seed'):
            with using_attributes(metadata={'memex.stage': 'reflection.seed'}):
                # This is what LiteLLMInstrumentor does on line 149:
                # name="acompletion", attributes=dict(get_attributes_from_context())
                with tracer.start_as_current_span(
                    'acompletion', attributes=dict(get_attributes_from_context())
                ):
                    pass  # simulates the LLM call

        spans = exporter.get_finished_spans()
        assert len(spans) == 2

        child = next(s for s in spans if s.name == 'acompletion')
        parent = next(s for s in spans if s.name == 'reflection.seed')

        # Verify parent-child relationship
        assert child.parent is not None
        assert child.parent.span_id == parent.context.span_id

        # Verify metadata attribute on child span
        assert child.attributes.get('metadata') == '{"memex.stage": "reflection.seed"}'
    finally:
        provider.shutdown()


async def test_async_context_propagation():
    """Verify using_attributes context is visible inside an awaited coroutine.

    This closes the async propagation gap — the predictor.acall() runs as an
    awaited coroutine inside the using_attributes context manager.
    """
    if not oi_available:
        pytest.skip('openinference not installed')

    from openinference.instrumentation import get_attributes_from_context, using_attributes

    captured = {}

    async def fake_acall(**kwargs):
        captured['attrs'] = dict(get_attributes_from_context())
        return MagicMock()

    pred = MagicMock()
    pred.acall = fake_acall

    old_using = llm_mod._oi_using_attributes
    llm_mod._oi_using_attributes = using_attributes  # use real using_attributes

    try:
        await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=pred,
            input_kwargs={},
            operation_name='extraction.facts',
        )

        assert 'attrs' in captured
        assert captured['attrs'].get('metadata') == '{"memex.stage": "extraction.facts"}'
    finally:
        llm_mod._oi_using_attributes = old_using
