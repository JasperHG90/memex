import datetime as dt
import pytest
import dspy
from sqlmodel import select, col
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import ExtractionConfig, SimpleTextSplitting, ConfidenceConfig, ModelConfig
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.models import RetainContent
from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.extraction.core import (
    ExtractSemanticFacts,
)
from memex_core.memory.sql_models import MemoryUnit


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_opinion_extraction(session: AsyncSession):
    """
    Integration test for:
    1. Extracting Opinions (using extract_opinions=True)
    """

    # --- Setup Components ---
    config = ExtractionConfig(
        model=ModelConfig(model='gemini/gemini-3-flash-preview'),
        text_splitting=SimpleTextSplitting(chunk_size_tokens=2000, chunk_overlap_tokens=200),
        max_concurrency=2,
    )
    lm = dspy.LM(model=config.model.model)
    predictor = dspy.Predict(ExtractSemanticFacts)
    embedding_model = await get_embedding_model()
    entity_resolver = EntityResolver(resolution_threshold=0.65)

    extractor = ExtractionEngine(
        config=config,
        confidence_config=ConfidenceConfig(),
        lm=lm,
        predictor=predictor,
        embedding_model=embedding_model,
        entity_resolver=entity_resolver,
    )

    # --- Part 1: Opinion Extraction ---

    # Text rich in opinions
    opinion_text = (
        'I firmly believe that Python is the best programming language for AI. '
        'I hate Java because it is too verbose. '
        'In my experience, microservices are often over-engineered for small teams.'
    )

    content = RetainContent(
        content=opinion_text,
        event_date=dt.datetime.now(dt.timezone.utc),
        payload={'source': 'developer_blog', 'author': 'DevGuru'},
        context='Tech opinions',
    )

    # Extract ONLY opinions
    opinion_unit_ids, _, _ = await extractor.extract_and_persist(
        session=session, contents=[content], agent_name='opinion_tester', extract_opinions=True
    )

    assert len(opinion_unit_ids) > 0, 'Should have extracted some opinions'

    # Verify fact type
    stmt_opinions = select(MemoryUnit).where(col(MemoryUnit.id).in_(opinion_unit_ids))
    result_opinions = await session.exec(stmt_opinions)
    opinions = result_opinions.all()

    for op in opinions:
        assert op.fact_type == 'opinion', f"Expected 'opinion', got {op.fact_type}"
        # Basic content check
        assert 'Python' in op.text or 'Java' in op.text or 'microservices' in op.text
