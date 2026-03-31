"""Tests that trace_span creates parent spans that group child operations."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import memex_core.llm as llm_mod
from memex_core.llm import run_dspy_operation
from memex_core.tracing import trace_span

# Guard imports — skip entire module if tracing extras missing
otel_available = True
try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:
    otel_available = False

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """Ensure a fresh circuit breaker for each test."""
    from memex_core.circuit_breaker import CircuitBreaker

    llm_mod._circuit_breaker = CircuitBreaker()


def _make_dummy_lm():
    from dspy.utils.dummies import DummyLM

    return DummyLM([{'response': 'ok'}])


def _make_mock_predictor(return_value=None):
    pred = MagicMock()
    pred.acall = AsyncMock(return_value=return_value or MagicMock())
    return pred


@pytest.fixture
def otel_env():
    """Fixture providing InMemorySpanExporter wired to both trace_span and llm_mod._tracer.

    Patches opentelemetry.trace.get_tracer so trace_span picks up our test provider,
    and sets llm_mod._tracer directly for run_dspy_operation.
    """
    if not otel_available:
        pytest.skip('opentelemetry SDK not installed')

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Wire llm_mod._tracer for run_dspy_operation
    old_tracer = llm_mod._tracer
    llm_mod._tracer = provider.get_tracer('memex.llm')

    # Patch get_tracer so trace_span uses our test provider
    with patch.object(otel_trace, 'get_tracer', side_effect=provider.get_tracer):
        yield exporter

    llm_mod._tracer = old_tracer
    provider.shutdown()


async def test_trace_span_creates_parent_span(otel_env):
    """trace_span creates a named span with attributes."""
    with trace_span(
        'memex.test',
        'reflection',
        {
            'reflection.entity_id': 'abc-123',
            'reflection.entity_name': 'Test Entity',
        },
    ):
        pass

    spans = otel_env.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == 'reflection'
    assert span.attributes['reflection.entity_id'] == 'abc-123'
    assert span.attributes['reflection.entity_name'] == 'Test Entity'


async def test_trace_span_nests_child_operations(otel_env):
    """Child spans from run_dspy_operation nest under a trace_span parent."""
    with trace_span('memex.reflection', 'reflection'):
        await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=_make_mock_predictor(),
            input_kwargs={},
            operation_name='reflection.seed',
        )
        await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=_make_mock_predictor(),
            input_kwargs={},
            operation_name='reflection.validate',
        )

    spans = otel_env.get_finished_spans()
    span_names = {s.name for s in spans}
    assert 'reflection' in span_names
    assert 'reflection.seed' in span_names
    assert 'reflection.validate' in span_names

    parent = next(s for s in spans if s.name == 'reflection')
    children = [s for s in spans if s.name in ('reflection.seed', 'reflection.validate')]

    for child in children:
        assert child.parent is not None
        assert child.parent.span_id == parent.context.span_id


async def test_trace_span_works_without_otel():
    """trace_span returns a no-op context manager when OTel is not installed."""
    with patch.dict('sys.modules', {'opentelemetry': None, 'opentelemetry.trace': None}):
        # Force ImportError path — trace_span catches it and returns nullcontext

        import memex_core.tracing as tracing_mod

        cm = tracing_mod.trace_span('memex.test', 'test_span')
        with cm:
            pass  # should not raise


async def test_trace_span_async_gather_propagation(otel_env):
    """Child spans created inside asyncio.gather tasks nest under trace_span parent.

    This validates the contradiction engine pattern where _process_flagged_unit
    tasks run concurrently via asyncio.gather.
    """

    async def simulated_task(op_name: str):
        await run_dspy_operation(
            lm=_make_dummy_lm(),
            predictor=_make_mock_predictor(),
            input_kwargs={},
            operation_name=op_name,
        )

    with trace_span('memex.contradiction', 'contradiction'):
        await asyncio.gather(
            simulated_task('contradiction.classify'),
            simulated_task('contradiction.classify'),
        )

    spans = otel_env.get_finished_spans()
    parent = next(s for s in spans if s.name == 'contradiction')
    children = [s for s in spans if s.name == 'contradiction.classify']

    assert len(children) == 2
    for child in children:
        assert child.parent is not None
        assert child.parent.span_id == parent.context.span_id


async def test_trace_span_does_not_swallow_exceptions(otel_env):
    """Exceptions raised inside trace_span propagate normally."""
    with pytest.raises(ValueError, match='boom'):
        with trace_span('memex.test', 'failing_span'):
            raise ValueError('boom')


async def test_nested_trace_spans(otel_env):
    """trace_span can be nested, producing a correct span tree."""
    with trace_span('memex.extraction', 'extraction'):
        with trace_span('memex.extraction', 'extraction.phase1'):
            await run_dspy_operation(
                lm=_make_dummy_lm(),
                predictor=_make_mock_predictor(),
                input_kwargs={},
                operation_name='extraction.facts',
            )

    spans = otel_env.get_finished_spans()
    root = next(s for s in spans if s.name == 'extraction')
    mid = next(s for s in spans if s.name == 'extraction.phase1')
    leaf = next(s for s in spans if s.name == 'extraction.facts')

    # extraction.phase1 is child of extraction
    assert mid.parent is not None
    assert mid.parent.span_id == root.context.span_id

    # extraction.facts is child of extraction.phase1
    assert leaf.parent is not None
    assert leaf.parent.span_id == mid.context.span_id


async def test_trace_span_with_no_attributes(otel_env):
    """trace_span works when no attributes are passed."""
    with trace_span('memex.test', 'simple_span'):
        pass

    spans = otel_env.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == 'simple_span'
