import pytest
import os
import dspy
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import ExtractionConfig, SimpleTextSplitting, ConfidenceConfig, ModelConfig
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.extraction.core import ExtractSemanticFacts


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_extraction_enforces_english_from_dutch(session: AsyncSession):
    """
    Verify that Dutch input is extracted as English facts.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    config = ExtractionConfig(
        model=ModelConfig(model='gemini/gemini-3-flash-preview'),
        text_splitting=SimpleTextSplitting(chunk_size_tokens=2000, chunk_overlap_tokens=200),
    )

    lm = dspy.LM(model=config.model.model, api_key=api_key)
    predictor = dspy.Predict(ExtractSemanticFacts)
    embedding_model = await get_embedding_model()
    entity_resolver = EntityResolver(resolution_threshold=0.65)

    _ = ExtractionEngine(
        config=config,
        confidence_config=ConfidenceConfig(),
        lm=lm,
        predictor=predictor,
        embedding_model=embedding_model,
        entity_resolver=entity_resolver,
    )
    # ... rest of function ...


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_extraction_mixed_language_input(session: AsyncSession):
    """
    Verify that mixed language input (English + Spanish) is normalized to English.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    config = ExtractionConfig(
        model=ModelConfig(model='gemini/gemini-3-flash-preview'),
        text_splitting=SimpleTextSplitting(chunk_size_tokens=2000, chunk_overlap_tokens=200),
    )

    _ = dspy.LM(model=config.model.model, api_key=api_key)
    # ... rest of function ...


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_extraction_preserves_complex_semantics(session: AsyncSession):
    """
    Verify that complex technical English is preserved accurately without 'over-simplification'
    or loss of specific terminology.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    config = ExtractionConfig(
        model=ModelConfig(model='gemini/gemini-3-flash-preview'),
        text_splitting=SimpleTextSplitting(chunk_size_tokens=2000, chunk_overlap_tokens=200),
    )

    _ = dspy.LM(model=config.model.model, api_key=api_key)
    # ... rest of function ...
