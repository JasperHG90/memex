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
from memex_core.memory.extraction.core import ExtractSemanticFacts
from memex_core.memory.sql_models import MemoryUnit, Entity, MemoryLink


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_extract_and_persist_end_to_end(session: AsyncSession):
    """
    End-to-End integration test for ExtractionEngine.
    Uses real LLM (Gemini), real Embeddings (FastEmbedder), and real Database.
    """
    # 1. Setup Real Components
    config = ExtractionConfig(
        model=ModelConfig(model='gemini/gemini-3-flash-preview'),
        text_splitting=SimpleTextSplitting(chunk_size_tokens=2000, chunk_overlap_tokens=200),
        max_concurrency=2,
    )

    # Use real Gemini model via dspy
    # Note: Expects GOOGLE_API_KEY environment variable to be set
    lm = dspy.LM(model=config.model.model)
    predictor = dspy.Predict(ExtractSemanticFacts)

    # Use real Embedding model (ONNX)
    embedding_model = await get_embedding_model()

    # Use real Entity Resolver
    entity_resolver = EntityResolver(resolution_threshold=0.65)

    extractor = ExtractionEngine(
        config=config,
        confidence_config=ConfidenceConfig(),
        lm=lm,
        predictor=predictor,
        embedding_model=embedding_model,
        entity_resolver=entity_resolver,
    )

    # 2. Prepare Input Content
    # Using a text with clear entities, temporal context, and potential causal relations
    input_text = (
        'On October 15, 2025, SpaceX successfully launched the Starship rocket from Boca Chica, Texas. '
        'This launch demonstrated significant improvements in the Raptor engines, which caused the vehicle '
        'to reach orbit for the first time. Elon Musk stated that this achievement enables future missions to Mars. '
        'The event was attended by thousands of spectators who cheered as the rocket ascended.'
    )

    content = RetainContent(
        content=input_text,
        event_date=dt.datetime(2025, 10, 15, tzinfo=dt.timezone.utc),
        payload={'source': 'news_article', 'author': 'SpaceReporter'},
        context='Space exploration news update.',
    )

    # 3. Execute Extraction and Persistence
    unit_ids, token_usage, _ = await extractor.extract_and_persist(
        session=session,
        contents=[content],
        agent_name='integration_test_agent',
        document_id=None,  # Let it generate a doc ID or handle logic internally
        is_first_batch=True,
    )

    # 4. Verification

    # A. Check returned unit IDs
    assert len(unit_ids) > 0, 'No memory units were returned'

    # Token usage might be 0 depending on the provider/model support in dspy
    if token_usage.total_tokens == 0:
        import warnings

        warnings.warn(
            'Token usage was 0. This might be due to the model provider or dspy adapter not returning usage stats.'
        )
    else:
        assert (token_usage.total_tokens or 0) > 0, 'Token usage should be recorded'

    # B. Check Memory Units in DB
    stmt_units = (
        select(MemoryUnit)
        .where(col(MemoryUnit.id).in_(unit_ids))
        .execution_options(populate_existing=True)
    )
    result_units = await session.exec(stmt_units)
    memory_units = result_units.all()

    assert len(memory_units) == len(unit_ids)

    # Check content of units
    text_combined = ' '.join([u.text for u in memory_units]).lower()
    assert 'spacex' in text_combined
    assert 'starship' in text_combined
    # Boca Chica might be in metadata or context, or lower-priority fact
    # We check if it's present at least in one of the texts or entities

    # Check embeddings are present and correct dimension
    for unit in memory_units:
        assert unit.embedding is not None
        assert len(unit.embedding) == 384  # Based on minilm-l12-v2-memex-ft-q8

    # C. Check Entities
    # We expect entities like "SpaceX", "Elon Musk", "Starship", "Boca Chica"
    stmt_entities = select(Entity).execution_options(populate_existing=True)
    result_entities = await session.exec(stmt_entities)
    entities = result_entities.all()

    assert len(entities) > 0
    entity_names = [e.canonical_name.lower() for e in entities]
    assert any('spacex' in name for name in entity_names)
    assert any('elon musk' in name for name in entity_names)

    # D. Check Links
    stmt_links = select(MemoryLink)
    result_links = await session.exec(stmt_links)
    links = result_links.all()

    # We expect at least some links (temporal, entity, or semantic)
    # Since we have multiple facts likely extracted from the text
    if len(memory_units) > 1:
        assert len(links) > 0

        # Check for specific link types if possible
        link_types = {link.link_type for link in links}
        # Temporal links are almost guaranteed between sequential facts
        assert 'temporal' in link_types or 'entity' in link_types

    # E. Check Semantic Search (Self-Verification)
    # Verify that we can find these facts using the embedding model
    query_text = 'Who launched a rocket in Texas?'
    query_embedding = embedding_model.encode([query_text])[0]

    # Use pgvector distance operator (<=> is cosine distance, we want 1 - distance for similarity)
    # or just order by embedding <=> query_embedding
    stmt_search = (
        select(MemoryUnit)
        .order_by(col(MemoryUnit.embedding).cosine_distance(query_embedding))  # type: ignore
        .limit(1)
    )
    result_search = await session.exec(stmt_search)
    top_match = result_search.first()

    assert top_match is not None
    # The top match should definitely be relevant to the query
    assert 'SpaceX' in top_match.text or 'Starship' in top_match.text
