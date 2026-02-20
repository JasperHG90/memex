from typing import cast

import pytest
import numpy as np
import pathlib as plb

from platformdirs import user_cache_dir

from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.reranking import get_reranking_model
from memex_core.memory.models.ner import get_ner_model

# Import models to ensure they are registered in SQLModel.metadata for table creation
from memex_core.memory.sql_models import Vault, Entity  # noqa: F401

# Paths to real model artifacts
EMBEDDING_MODEL_PATH = (
    plb.Path(user_cache_dir('memex')) / 'JasperHG90__minilm-l12-v2-hindsight-embeddings'
)
RERANKING_MODEL_PATH = (
    plb.Path(user_cache_dir('memex')) / 'JasperHG90__ms-marco-minilm-l12-hindsight-reranker'
)
NER_MODEL_PATH = plb.Path(user_cache_dir('memex')) / 'JasperHG90__distilbert-hindsight-ner'


@pytest.mark.integration
@pytest.mark.skipif(not EMBEDDING_MODEL_PATH.exists(), reason='Embedding model artifacts not found')
class TestEmbeddingIntegration:
    """Integration tests for the embedding model using real artifacts."""

    async def test_single_embedding_inference(self) -> None:
        """Verify that a single text can be embedded and produces a valid vector."""
        model = await get_embedding_model()
        text = 'This is a test of the emergency broadcast system.'

        vector = model.encode([text])

        # Verify shape (batch=1, hidden=384 for MiniLM-L12)
        assert vector.shape == (1, 384)
        assert not np.isnan(vector).any()
        # Verify it's normalized or at least non-zero
        assert np.linalg.norm(vector) > 0

    async def test_batch_embedding_inference(self) -> None:
        """Verify that multiple documents can be embedded in a single batch."""
        model = await get_embedding_model()
        texts = [
            'The quick brown fox jumps over the lazy dog.',
            'Artificial intelligence is transforming software engineering.',
            'A standard batch of documents for embedding.',
        ]

        vectors = model.encode(texts)

        assert vectors.shape == (3, 384)
        assert not np.isnan(vectors).any()

        # Verify that different texts produce different embeddings
        cos_sim = np.dot(vectors[0], vectors[1]) / (
            np.linalg.norm(vectors[0]) * np.linalg.norm(vectors[1])
        )
        assert cos_sim < 0.99  # They should be distinct


@pytest.mark.integration
@pytest.mark.skipif(not RERANKING_MODEL_PATH.exists(), reason='Reranking model artifacts not found')
class TestRerankingIntegration:
    """Integration tests for the reranking model using real artifacts."""

    async def test_reranking_logic(self) -> None:
        """Verify that the reranker correctly scores and ranks relevant vs irrelevant texts."""
        model = await get_reranking_model()

        query = 'How do I install Python dependencies?'

        # A highly relevant doc, a somewhat relevant doc, and noise
        texts = [
            'To install dependencies in Python, you usually use pip install -r requirements.txt or uv sync.',
            'The weather in Amsterdam is quite rainy in the winter months.',
            'Python is a versatile programming language used for data science and web development.',
        ]
        doc_ids = ['relevant', 'noise', 'semi-relevant']

        results = model.rerank(query, texts, doc_ids)

        # Verify we got 3 results
        assert len(results) == 3

        # The most relevant document should be at the top
        assert results[0]['id'] == 'relevant'
        assert cast(float, results[0]['score']) > cast(float, results[1]['score'])

        # The noise document should be at the bottom
        assert results[-1]['id'] == 'noise'

        # Verify dictionary structure
        assert 'text' in results[0]
        assert 'score' in results[0]
        assert isinstance(results[0]['score'], float)


@pytest.mark.integration
@pytest.mark.skipif(not NER_MODEL_PATH.exists(), reason='NER model artifacts not found')
class TestNERIntegration:
    """Integration tests for the NER model using real artifacts."""

    async def test_ner_extraction(self) -> None:
        """Verify that entities are correctly extracted from text."""
        model = await get_ner_model()
        text = 'Apple Inc. is located in Cupertino, California.'

        entities = model.predict(text)

        # We expect at least Apple (ORG) and Cupertino (LOC) and California (LOC)
        # exact output depends on the model's training, but we can check for non-empty results
        assert len(entities) > 0

        # Check structure
        first_entity = entities[0]
        assert 'word' in first_entity
        assert 'type' in first_entity
        assert 'start' in first_entity
        assert 'end' in first_entity
        assert 'score' in first_entity

        # Check for expected entities loosely
        words = [e['word'] for e in entities]
        # flexible check in case "Apple Inc." is one entity or "Apple" is one.
        assert any('Apple' in w for w in words)
        assert any('California' in w for w in words)

    async def test_empty_input(self) -> None:
        """Verify behavior with empty input."""
        model = await get_ner_model()
        entities = model.predict('')
        assert entities == []
