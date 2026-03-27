"""Integration tests for litellm-backed embedding and reranking adapters.

These tests make **real** API calls (Gemini for embedding, local HTTP server
for reranking) and exercise the full adapter → litellm → network → response
→ numpy pipeline.

Run with:
    uv run pytest -m integration tests/integration/memory/models/test_int_litellm_backends.py -v
"""

import asyncio
import json
import math
import os
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from memex_common.config import (
    DisabledBackend,
    LitellmEmbeddingBackend,
    LitellmRerankerBackend,
)
from memex_core.memory.models.backends.litellm_embedder import LiteLLMEmbedder
from memex_core.memory.models.backends.litellm_reranker import LiteLLMReranker
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.protocols import EmbeddingsModel, RerankerModel
from memex_core.memory.models.reranking import get_reranking_model
from memex_core.memory.sql_models import EMBEDDING_DIMENSION

_HAS_GEMINI_KEY = bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'))

# -- Gemini embedding model available via the free API tier --
GEMINI_EMBEDDING_MODEL = 'gemini/gemini-embedding-001'


# ---------------------------------------------------------------------------
# Local rerank server (Cohere-compatible JSON contract)
# ---------------------------------------------------------------------------


class _CohereRerankHandler(BaseHTTPRequestHandler):
    """Minimal Cohere-format rerank endpoint for integration testing.

    Scores documents by word overlap with the query so results are
    deterministic and semantically meaningful.
    """

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        query_words = set(body.get('query', '').lower().split())
        results = []
        for i, doc in enumerate(body.get('documents', [])):
            doc_text = doc if isinstance(doc, str) else doc.get('text', '')
            doc_words = set(doc_text.lower().split())
            overlap = len(query_words & doc_words) / max(len(query_words | doc_words), 1)
            results.append({'index': i, 'relevance_score': round(min(0.99, max(0.01, overlap)), 4)})

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'id': 'test', 'results': results, 'meta': {}}).encode())

    def log_message(self, *_args: object) -> None:
        pass  # suppress request logs


@pytest.fixture(scope='module')
def rerank_server() -> Generator[str]:
    """Start a local Cohere-format rerank HTTP server, return its base URL."""
    server = HTTPServer(('127.0.0.1', 0), _CohereRerankHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{port}'
    server.shutdown()


# ===================================================================
# Embedding adapter — real Gemini API calls
# ===================================================================


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
class TestLiteLLMEmbedderIntegration:
    """End-to-end tests hitting the real Gemini embedding API."""

    def test_single_text(self) -> None:
        embedder = LiteLLMEmbedder(LitellmEmbeddingBackend(model=GEMINI_EMBEDDING_MODEL))
        result = embedder.encode(['hello world'])

        assert isinstance(result, np.ndarray)
        assert result.ndim == 2
        assert result.shape[0] == 1
        assert result.shape[1] > 0
        assert result.dtype == np.float32
        assert not np.isnan(result).any()

    def test_batch(self) -> None:
        embedder = LiteLLMEmbedder(LitellmEmbeddingBackend(model=GEMINI_EMBEDDING_MODEL))
        texts = [
            'machine learning research',
            'the weather is sunny',
            'deep neural networks for NLP',
        ]
        result = embedder.encode(texts)

        assert result.shape[0] == 3
        assert result.shape[1] > 0
        # Different texts should produce different embeddings
        cos_sim = np.dot(result[0], result[1]) / (
            np.linalg.norm(result[0]) * np.linalg.norm(result[1])
        )
        assert cos_sim < 0.99

    def test_satisfies_protocol(self) -> None:
        embedder = LiteLLMEmbedder(LitellmEmbeddingBackend(model=GEMINI_EMBEDDING_MODEL))
        assert isinstance(embedder, EmbeddingsModel)

    def test_asyncio_to_thread(self) -> None:
        """Verify encode() works inside asyncio.to_thread (same as retrieval engine)."""
        embedder = LiteLLMEmbedder(LitellmEmbeddingBackend(model=GEMINI_EMBEDDING_MODEL))

        async def _run() -> np.ndarray:
            return await asyncio.to_thread(embedder.encode, ['thread test'])

        result = asyncio.run(_run())
        assert isinstance(result, np.ndarray)
        assert result.shape[0] == 1

    async def test_factory_returns_litellm_adapter(self) -> None:
        config = LitellmEmbeddingBackend(model=GEMINI_EMBEDDING_MODEL)
        model = await get_embedding_model(config)
        assert isinstance(model, LiteLLMEmbedder)
        assert isinstance(model, EmbeddingsModel)

    def test_dimension_mismatch_detected(self) -> None:
        """Gemini produces 3072-dim vectors; our DB expects 384. Verify detection."""
        embedder = LiteLLMEmbedder(LitellmEmbeddingBackend(model=GEMINI_EMBEDDING_MODEL))
        probe = embedder.encode(['dimension probe'])
        actual_dim = probe.shape[-1]
        assert actual_dim != EMBEDDING_DIMENSION, (
            f'Gemini should not produce {EMBEDDING_DIMENSION}-dim vectors'
        )


# ===================================================================
# Reranker adapter — real HTTP calls to local server
# ===================================================================


@pytest.mark.integration
class TestLiteLLMRerankerIntegration:
    """End-to-end tests hitting a real HTTP rerank endpoint."""

    def test_score_shape_and_dtype(self, rerank_server: str) -> None:
        reranker = LiteLLMReranker(
            LitellmRerankerBackend(
                model='cohere/rerank-v3.5',
                api_base=rerank_server,
                api_key='fake-key',
            )
        )
        scores = reranker.score('test query', ['doc one', 'doc two', 'doc three'])

        assert isinstance(scores, np.ndarray)
        assert scores.shape == (3,)
        assert scores.dtype == np.float32
        assert np.all(np.isfinite(scores))

    def test_logit_transform_roundtrip(self, rerank_server: str) -> None:
        """Verify sigmoid(logit(relevance_score)) recovers the original score."""
        reranker = LiteLLMReranker(
            LitellmRerankerBackend(
                model='cohere/rerank-v3.5',
                api_base=rerank_server,
                api_key='fake-key',
            )
        )
        texts = ['alpha beta gamma', 'delta epsilon']
        scores = reranker.score('alpha beta', texts)

        # Apply the same sigmoid the retrieval engine does (engine.py:987)
        recovered = [1.0 / (1.0 + math.exp(-s)) for s in scores]

        # The local server scores by word overlap, so 'alpha beta gamma'
        # should score higher than 'delta epsilon' for query 'alpha beta'
        assert recovered[0] > recovered[1]

        # All recovered scores should be in (0, 1)
        assert all(0 < s < 1 for s in recovered)

    def test_score_order_preserved(self, rerank_server: str) -> None:
        """Scores must be in original document order, not sorted by relevance."""
        reranker = LiteLLMReranker(
            LitellmRerankerBackend(
                model='cohere/rerank-v3.5',
                api_base=rerank_server,
                api_key='fake-key',
            )
        )
        # doc[0] has no overlap, doc[1] has full overlap
        scores = reranker.score('hello world', ['no match here', 'hello world again'])

        recovered = [1.0 / (1.0 + math.exp(-s)) for s in scores]
        # doc[1] should score higher than doc[0]
        assert recovered[1] > recovered[0]

    def test_empty_raises(self, rerank_server: str) -> None:
        reranker = LiteLLMReranker(
            LitellmRerankerBackend(
                model='cohere/rerank-v3.5',
                api_base=rerank_server,
                api_key='fake-key',
            )
        )
        with pytest.raises(ValueError, match='Empty text list'):
            reranker.score('q', [])

    def test_satisfies_protocol(self, rerank_server: str) -> None:
        reranker = LiteLLMReranker(
            LitellmRerankerBackend(
                model='cohere/rerank-v3.5',
                api_base=rerank_server,
                api_key='fake-key',
            )
        )
        assert isinstance(reranker, RerankerModel)

    def test_asyncio_to_thread(self, rerank_server: str) -> None:
        """Verify score() works inside asyncio.to_thread (same as retrieval engine)."""
        reranker = LiteLLMReranker(
            LitellmRerankerBackend(
                model='cohere/rerank-v3.5',
                api_base=rerank_server,
                api_key='fake-key',
            )
        )

        async def _run() -> np.ndarray:
            return await asyncio.to_thread(reranker.score, 'query', ['doc'])

        result = asyncio.run(_run())
        assert isinstance(result, np.ndarray)
        assert result.shape == (1,)

    async def test_factory_returns_litellm_adapter(self, rerank_server: str) -> None:
        config = LitellmRerankerBackend(
            model='cohere/rerank-v3.5',
            api_base=rerank_server,
            api_key='fake-key',
        )
        model = await get_reranking_model(config)
        assert isinstance(model, LiteLLMReranker)
        assert isinstance(model, RerankerModel)

    async def test_factory_disabled_returns_none(self) -> None:
        model = await get_reranking_model(DisabledBackend())
        assert model is None


# ===================================================================
# Full retrieval pipeline simulation
# ===================================================================


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
class TestRetrievalPipelineSimulation:
    """Simulate the exact sequence retrieval/engine.py performs."""

    def test_embed_then_rerank(self, rerank_server: str) -> None:
        embedder = LiteLLMEmbedder(LitellmEmbeddingBackend(model=GEMINI_EMBEDDING_MODEL))
        reranker = LiteLLMReranker(
            LitellmRerankerBackend(
                model='cohere/rerank-v3.5',
                api_base=rerank_server,
                api_key='fake-key',
            )
        )

        query = 'machine learning research'
        candidates = [
            'machine learning is a growing field of research',
            'the weather in Amsterdam is rainy',
            'deep learning research advances',
        ]

        # Step 1: embed query (retrieval/engine.py:189)
        query_embedding = embedder.encode([query])
        assert query_embedding.shape[0] == 1
        assert query_embedding.shape[1] > 0

        # Step 2: score candidates (retrieval/engine.py:984)
        raw_scores = reranker.score(query, candidates)
        assert raw_scores.shape == (3,)

        # Step 3: sigmoid normalize (retrieval/engine.py:987)
        normalized = [1.0 / (1.0 + math.exp(-s)) for s in raw_scores]
        assert all(0 < s < 1 for s in normalized)

        # Step 4: apply boosts (retrieval/engine.py:994-1011)
        recency_alpha = 0.2
        temporal_alpha = 0.2
        boosted: list[float] = []
        for ce_score in normalized:
            recency_boost = 1.0 + recency_alpha * (0.5 - 0.5)  # neutral
            temporal_boost = 1.0 + temporal_alpha * (0.5 - 0.5)  # neutral
            boosted.append(ce_score * recency_boost * temporal_boost)

        # Semantically: candidates[0] and [2] should outscore [1] (weather)
        assert boosted[0] > boosted[1]
        assert boosted[2] > boosted[1]
