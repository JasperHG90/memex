"""Integration tests for F2 anisotropy correction — AnisotropyCorrector wired into retrieval.

Verifies that the AnisotropyCorrector is instantiated in the RetrievalEngine,
configured from RetrievalConfig, and normalizes cosine similarity scores during
pairwise computation for MMR.
"""

import pytest
import pytest_asyncio

from memex_common.config import RetrievalConfig
from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.models.anisotropy import AnisotropyCorrector


@pytest.mark.integration
class TestAnisotropyIntegration:
    @pytest_asyncio.fixture(scope='class')
    async def embedder(self):
        return await get_embedding_model()

    async def test_anisotropy_corrector_wired_into_engine(self, embedder):
        engine = RetrievalEngine(embedder=embedder)
        assert hasattr(engine, '_anisotropy')
        assert isinstance(engine._anisotropy, AnisotropyCorrector)

    async def test_anisotropy_cold_start_passthrough(self, embedder):
        engine = RetrievalEngine(embedder=embedder)
        corrector = engine._anisotropy

        # Before any observations, normalize returns raw score
        raw = 0.85
        result = corrector.normalize(raw)
        assert result == raw  # Cold-start passthrough

    async def test_anisotropy_normalization_after_warmup(self, embedder):
        config = RetrievalConfig(anisotropy_min_samples=5, anisotropy_window_size=100)
        engine = RetrievalEngine(embedder=embedder, retrieval_config=config)
        corrector = engine._anisotropy

        # Feed enough observations to activate
        for i in range(5):
            corrector.normalize(0.8 + 0.01 * i)

        # Now normalization should be active
        raw = 0.82
        result = corrector.normalize(raw)
        # After warmup, result should be sigmoid-transformed
        assert 0 < result < 1
        # Result should differ from raw (unless degenerate case)
        assert result != raw

    async def test_anisotropy_config_propagation(self, embedder):
        config = RetrievalConfig(anisotropy_window_size=256, anisotropy_min_samples=16)
        engine = RetrievalEngine(embedder=embedder, retrieval_config=config)

        assert engine._anisotropy.window_size == 256
        assert engine._anisotropy._min_samples == 16

    async def test_anisotropy_disabled_mode(self, embedder):
        config = RetrievalConfig(anisotropy_window_size=0)
        engine = RetrievalEngine(embedder=embedder, retrieval_config=config)

        raw = 0.85
        result = engine._anisotropy.normalize(raw)
        assert result == raw  # Disabled: passthrough

    async def test_anisotropy_default_config(self, embedder):
        engine = RetrievalEngine(embedder=embedder)

        assert engine._anisotropy.window_size == 1024
        assert engine._anisotropy._min_samples == 32
