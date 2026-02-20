from unittest.mock import AsyncMock, MagicMock
from typing import Generator

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from memex_core.config import ExtractionConfig, SimpleTextSplitting, ModelConfig
from memex_core.memory.extraction.models import RawFact
from memex_core.types import FactTypes, FactKindTypes


@pytest.fixture
def mock_lm() -> Generator[DummyLM, None, None]:
    """Fixture to provide a configured dspy.DummyLM."""
    lm = DummyLM([])
    with dspy.context(lm=lm):
        yield lm


@pytest.fixture
def mock_predictor() -> MagicMock:
    """Fixture to provide a mocked dspy.Predictor."""
    predictor = MagicMock(spec=dspy.Predict)
    predictor.acall = AsyncMock()
    return predictor


@pytest.fixture
def sample_raw_fact() -> RawFact:
    """Fixture to provide a sample RawFact."""
    return RawFact(
        what='Test fact',
        fact_type=FactTypes.WORLD,
        fact_kind=FactKindTypes.CONVERSATION,
    )


@pytest.fixture
def config() -> ExtractionConfig:
    """Fixture to provide a default ExtractionConfig."""
    return ExtractionConfig(
        model=ModelConfig(model='gemini/gemini-pro'),
        text_splitting=SimpleTextSplitting(chunk_size_tokens=1000, chunk_overlap_tokens=100),
        max_concurrency=2,
    )
