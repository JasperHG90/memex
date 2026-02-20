from datetime import datetime, timezone
from typing import Any

import pytest
import numpy as np

from memex_core.memory.extraction import embedding_processor
from memex_core.memory.extraction.models import ExtractedFact, FactTypes


from memex_core.memory.extraction.embedding_processor import EmbeddingsModel


class MockEmbedder(EmbeddingsModel):
    def encode(self, text: list[str]) -> Any:
        # Returns dummy embeddings: 1.0 for all dimensions
        # Shape: (len(text), 384)
        return np.ones((len(text), 384), dtype=np.float32)


@pytest.fixture
def sample_fact():
    return ExtractedFact(
        fact_text='Test fact',
        fact_type=FactTypes.WORLD,
        content_index=0,
        chunk_index=0,
        mentioned_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
        occurred_start=datetime(2023, 1, 1, tzinfo=timezone.utc),
    )


def test_format_facts_for_embedding(sample_fact):
    facts = [sample_fact]
    formatted = embedding_processor.format_facts_for_embedding(facts)
    assert len(formatted) == 1
    # Standard format: "{Type}: {Text}" (since sample_fact.context is None)
    assert formatted[0] == 'World: Test fact'


def test_format_facts_for_embedding_with_context(sample_fact):
    sample_fact.context = 'Historical'
    facts = [sample_fact]
    formatted = embedding_processor.format_facts_for_embedding(facts)
    assert len(formatted) == 1
    # Standard format: "{Type} ({Context}): {Text}"
    assert formatted[0] == 'World (Historical): Test fact'


def test_generate_embedding_sync():
    mock_model = MockEmbedder()
    embedding = embedding_processor.generate_embedding(mock_model, 'test text')
    assert len(embedding) == 384
    assert embedding[0] == 1.0
    assert isinstance(embedding, list)


@pytest.mark.asyncio
async def test_generate_embeddings_batch_async():
    mock_model = MockEmbedder()
    texts = ['text 1', 'text 2']
    embeddings = await embedding_processor.generate_embeddings_batch(mock_model, texts)

    assert len(embeddings) == 2
    assert len(embeddings[0]) == 384
    assert len(embeddings[1]) == 384
    assert isinstance(embeddings[0], list)


@pytest.mark.asyncio
async def test_generate_embeddings_batch_empty():
    mock_model = MockEmbedder()
    embeddings = await embedding_processor.generate_embeddings_batch(mock_model, [])
    assert embeddings == []
