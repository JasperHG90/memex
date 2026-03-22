"""LLM mocking fixtures for CI tests that don't require an LLM API key.

Usage:
    Mark tests with ``@pytest.mark.llm_mock`` and request the ``mock_dspy_lm``
    fixture.  The fixture patches ``run_dspy_operation`` at the DSPy layer so
    that no network calls are made.

    For custom golden outputs, pass a mapping to ``mock_dspy_lm`` via indirect
    parametrize or call ``mock_dspy_lm.set_responses(...)`` inside the test.

Example::

    @pytest.mark.llm_mock
    async def test_extract(mock_dspy_lm):
        mock_dspy_lm.set_responses([golden_extraction_result])
        ...
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from memex_core.memory.extraction.models import (
    ExtractedOutput,
    RawFact,
)
from memex_core.memory.reflect.prompts import (
    CandidateObservation,
)
from memex_core.types import FactTypes, FactKindTypes


# ---------------------------------------------------------------------------
# Golden outputs: Extraction
# ---------------------------------------------------------------------------

GOLDEN_EXTRACTION_FACTS: list[RawFact] = [
    RawFact(
        what='Python is a popular programming language',
        fact_type=FactTypes.WORLD,
        fact_kind=FactKindTypes.CONVERSATION,
    ),
    RawFact(
        what='The user prefers Python for data analysis',
        fact_type=FactTypes.EVENT,
        fact_kind=FactKindTypes.CONVERSATION,
    ),
]

GOLDEN_EXTRACTION_OUTPUT = ExtractedOutput(
    extracted_facts=GOLDEN_EXTRACTION_FACTS,
)

# ---------------------------------------------------------------------------
# Golden outputs: Reflection — Seed Phase
# ---------------------------------------------------------------------------

GOLDEN_SEED_OBSERVATIONS: list[CandidateObservation] = [
    CandidateObservation(
        content='The user regularly uses Python for scripting and data analysis tasks',
        reasoning='Multiple memories reference Python usage for different tasks',
        evidence_indices=[0, 1],
    ),
]

# ---------------------------------------------------------------------------
# Mock LM wrapper
# ---------------------------------------------------------------------------


class MockDspyLM:
    """Configurable mock for DSPy LM calls.

    Wraps a ``DummyLM`` and provides helpers to set golden responses
    for ``run_dspy_operation``.
    """

    def __init__(self) -> None:
        self.dummy_lm = DummyLM([])
        self._responses: list[Any] = []
        self._call_index = 0
        self._run_dspy_patcher: AsyncMock | None = None

    # -- public API -----------------------------------------------------------

    def set_responses(self, responses: list[Any]) -> None:
        """Set the ordered list of results to return."""
        self._responses = list(responses)
        self._call_index = 0

    def add_response(self, result: Any) -> None:
        """Append a single response."""
        self._responses.append(result)

    @property
    def call_count(self) -> int:
        return self._call_index

    # -- internal -------------------------------------------------------------

    async def _mock_run_dspy(
        self,
        lm: Any,
        predictor: Any,
        input_kwargs: dict[str, Any],
        session: Any = None,
        context_metadata: dict | None = None,
        semaphore: Any = None,
        vault_id: Any = None,
    ) -> Any:
        """Side-effect function that replaces ``run_dspy_operation``."""
        if self._call_index < len(self._responses):
            result = self._responses[self._call_index]
        else:
            # Fall back to a generic empty result
            result = MagicMock()
        self._call_index += 1
        return result


# ---------------------------------------------------------------------------
# Pytest fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_dspy_lm(monkeypatch: pytest.MonkeyPatch) -> Generator[MockDspyLM, None, None]:
    """Fixture providing a ``MockDspyLM`` with ``run_dspy_operation`` patched.

    The fixture patches ``memex_core.llm.run_dspy_operation`` globally so any
    code path that calls it will use the mock.  Tests set golden outputs via
    ``mock_dspy_lm.set_responses(...)`` or ``mock_dspy_lm.add_response(...)``.

    A ``DummyLM`` is also activated as the default DSPy LM so that any code
    inspecting ``dspy.settings.lm`` finds a valid object.
    """
    mock = MockDspyLM()

    # Patch the central run_dspy_operation used everywhere
    monkeypatch.setattr(
        'memex_core.llm.run_dspy_operation',
        AsyncMock(side_effect=mock._mock_run_dspy),
    )

    # Activate DummyLM as default LM for code that reads dspy.settings.lm
    ctx = dspy.context(lm=mock.dummy_lm)
    ctx.__enter__()
    monkeypatch.setattr(mock, '_ctx', ctx)

    yield mock

    ctx.__exit__(None, None, None)
