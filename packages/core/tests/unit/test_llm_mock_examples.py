"""Example tests demonstrating the LLM mocking strategy for CI.

These tests use the ``mock_dspy_lm`` fixture to run code that would normally
require an LLM API key, without making any network calls.

Mark tests with ``@pytest.mark.llm_mock`` for filtering::

    uv run pytest -m llm_mock -v
"""

import datetime as dt
from unittest.mock import MagicMock

import pytest

from memex_core.memory.extraction.core import _extract_facts_from_chunk
from memex_core.memory.extraction.models import ExtractedOutput, RawFact
from memex_core.memory.sql_models import TokenUsage
from memex_core.types import FactTypes, FactKindTypes

# ---------------------------------------------------------------------------
# Golden outputs — reusable across tests
# ---------------------------------------------------------------------------

GOLDEN_FACTS: list[RawFact] = [
    RawFact(
        what='Python is a popular programming language',
        fact_type=FactTypes.WORLD,
        fact_kind=FactKindTypes.CONVERSATION,
    ),
    RawFact(
        what='The user prefers Python for data analysis',
        fact_type=FactTypes.EXPERIENCE,
        fact_kind=FactKindTypes.CONVERSATION,
    ),
]

GOLDEN_EXTRACTION = ExtractedOutput(
    extracted_facts=GOLDEN_FACTS,
)

GOLDEN_USAGE = TokenUsage(
    input_tokens=150,
    output_tokens=80,
    total_tokens=230,
    is_cached=False,
    models=['test-model/mock'],
)


def _make_extraction_result(output: ExtractedOutput) -> MagicMock:
    """Wrap an ExtractedOutput in a MagicMock matching run_dspy_operation's return."""
    result = MagicMock()
    result.extracted_facts = output
    return result


# ---------------------------------------------------------------------------
# Fact extraction with mocked LLM
# ---------------------------------------------------------------------------


@pytest.mark.llm_mock
@pytest.mark.asyncio
async def test_extract_facts_returns_golden_output(mock_dspy_lm):
    """Fact extraction returns the golden output when LLM is mocked."""
    mock_dspy_lm.set_responses(
        [
            (_make_extraction_result(GOLDEN_EXTRACTION), GOLDEN_USAGE),
        ]
    )

    facts, usage = await _extract_facts_from_chunk(
        chunk='Python is widely used for data science and scripting.',
        chunk_index=0,
        total_chunks=1,
        event_date=dt.datetime(2025, 6, 15),
        context='A conversation about programming languages',
        lm=mock_dspy_lm.dummy_lm,
        predictor=MagicMock(),
    )

    assert len(facts) == 2
    assert facts[0].what == 'Python is a popular programming language'
    assert facts[0].fact_type == FactTypes.WORLD
    assert facts[1].fact_type == FactTypes.EXPERIENCE
    assert usage.input_tokens == 150
    assert usage.total_tokens == 230
    assert mock_dspy_lm.call_count == 1


@pytest.mark.llm_mock
@pytest.mark.asyncio
async def test_extract_facts_multiple_chunks(mock_dspy_lm):
    """Multiple extraction calls consume responses in order."""
    chunk1_output = ExtractedOutput(
        extracted_facts=[
            RawFact(
                what='Fact from chunk 1',
                fact_type=FactTypes.WORLD,
                fact_kind=FactKindTypes.CONVERSATION,
            )
        ],
    )

    chunk2_output = ExtractedOutput(
        extracted_facts=[
            RawFact(
                what='Fact from chunk 2',
                fact_type=FactTypes.EXPERIENCE,
                fact_kind=FactKindTypes.CONVERSATION,
            )
        ],
    )

    mock_dspy_lm.set_responses(
        [
            (_make_extraction_result(chunk1_output), GOLDEN_USAGE),
            (_make_extraction_result(chunk2_output), GOLDEN_USAGE),
        ]
    )

    facts_1, _ = await _extract_facts_from_chunk(
        chunk='chunk one text',
        chunk_index=0,
        total_chunks=2,
        event_date=dt.datetime.now(),
        context='',
        lm=mock_dspy_lm.dummy_lm,
        predictor=MagicMock(),
    )

    facts_2, _ = await _extract_facts_from_chunk(
        chunk='chunk two text',
        chunk_index=1,
        total_chunks=2,
        event_date=dt.datetime.now(),
        context='',
        lm=mock_dspy_lm.dummy_lm,
        predictor=MagicMock(),
    )

    assert facts_1[0].what == 'Fact from chunk 1'
    assert facts_2[0].what == 'Fact from chunk 2'
    assert mock_dspy_lm.call_count == 2


@pytest.mark.llm_mock
@pytest.mark.asyncio
async def test_add_response_dynamically(mock_dspy_lm):
    """Responses can be added dynamically during the test."""
    single_fact = ExtractedOutput(
        extracted_facts=[
            RawFact(
                what='Dynamic fact',
                fact_type=FactTypes.WORLD,
                fact_kind=FactKindTypes.CONVERSATION,
            )
        ],
    )

    # Add a response after fixture creation
    mock_dspy_lm.add_response(_make_extraction_result(single_fact))

    facts, usage = await _extract_facts_from_chunk(
        chunk='some text',
        chunk_index=0,
        total_chunks=1,
        event_date=dt.datetime.now(),
        context='',
        lm=mock_dspy_lm.dummy_lm,
        predictor=MagicMock(),
    )

    assert len(facts) == 1
    assert facts[0].what == 'Dynamic fact'
    # Default golden usage is used when no custom usage is provided
    assert usage.input_tokens == 150
