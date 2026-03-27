"""Tests for inference model backend configuration."""

import os
from unittest.mock import patch

import pytest

from memex_common.config import (
    DisabledBackend,
    LitellmEmbeddingBackend,
    LitellmRerankerBackend,
    MemexConfig,
    OnnxBackend,
    RetrievalConfig,
    ServerConfig,
)


class TestDefaultConfig:
    """Default config should produce OnnxBackend for everything."""

    def test_server_embedding_default_is_onnx(self) -> None:
        sc = ServerConfig()
        assert isinstance(sc.embedding_model, OnnxBackend)
        assert sc.embedding_model.type == 'onnx'

    def test_retrieval_reranker_default_is_onnx(self) -> None:
        rc = RetrievalConfig()
        assert isinstance(rc.reranker, OnnxBackend)
        assert rc.reranker.type == 'onnx'


class TestLitellmEmbeddingConfig:
    def test_parse_minimal(self) -> None:
        config = LitellmEmbeddingBackend(model='openai/text-embedding-3-small')
        assert config.type == 'litellm'
        assert config.model == 'openai/text-embedding-3-small'
        assert config.api_base is None
        assert config.api_key is None
        assert config.dimensions is None

    def test_parse_full(self) -> None:
        config = LitellmEmbeddingBackend(
            model='ollama/nomic-embed-text',
            api_base='http://localhost:11434',
            api_key='sk-test',
            dimensions=384,
        )
        assert config.model == 'ollama/nomic-embed-text'
        assert str(config.api_base) == 'http://localhost:11434/'
        assert config.api_key.get_secret_value() == 'sk-test'
        assert config.dimensions == 384


class TestLitellmRerankerConfig:
    def test_parse_minimal(self) -> None:
        config = LitellmRerankerBackend(model='cohere/rerank-v3.5')
        assert config.type == 'litellm'
        assert config.model == 'cohere/rerank-v3.5'

    def test_parse_with_api_base(self) -> None:
        config = LitellmRerankerBackend(
            model='together_ai/Salesforce/Llama-Rank-V1',
            api_base='http://localhost:8080',
        )
        assert str(config.api_base) == 'http://localhost:8080/'


class TestDisabledBackend:
    def test_parse(self) -> None:
        config = DisabledBackend()
        assert config.type == 'disabled'


class TestEnvVarOverride:
    """Config should be overridable via env vars."""

    def test_embedding_model_via_env(self) -> None:
        env = {
            'MEMEX_SERVER__EMBEDDING_MODEL__TYPE': 'litellm',
            'MEMEX_SERVER__EMBEDDING_MODEL__MODEL': 'gemini/text-embedding-004',
        }
        with patch.dict(os.environ, env, clear=False):
            config = MemexConfig()
        assert isinstance(config.server.embedding_model, LitellmEmbeddingBackend)
        assert config.server.embedding_model.model == 'gemini/text-embedding-004'

    def test_reranker_disabled_via_env(self) -> None:
        env = {
            'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKER__TYPE': 'disabled',
        }
        with patch.dict(os.environ, env, clear=False):
            config = MemexConfig()
        assert isinstance(config.server.memory.retrieval.reranker, DisabledBackend)

    def test_reranker_litellm_via_env(self) -> None:
        env = {
            'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKER__TYPE': 'litellm',
            'MEMEX_SERVER__MEMORY__RETRIEVAL__RERANKER__MODEL': 'cohere/rerank-v3.5',
        }
        with patch.dict(os.environ, env, clear=False):
            config = MemexConfig()
        assert isinstance(config.server.memory.retrieval.reranker, LitellmRerankerBackend)
        assert config.server.memory.retrieval.reranker.model == 'cohere/rerank-v3.5'


class TestFactoryDispatch:
    """Verify factory functions dispatch correctly on config type."""

    @pytest.mark.asyncio
    async def test_get_embedding_model_onnx(self) -> None:
        from memex_core.memory.models.embedding import get_embedding_model, FastEmbedder

        with (
            patch('memex_core.memory.models.base.BaseOnnxModel.__init__', return_value=None),
            patch('pathlib.Path.exists', return_value=True),
        ):
            model = await get_embedding_model(OnnxBackend())
        assert isinstance(model, FastEmbedder)

    @pytest.mark.asyncio
    async def test_get_embedding_model_litellm(self) -> None:
        from memex_core.memory.models.embedding import get_embedding_model
        from memex_core.memory.models.backends.litellm_embedder import LiteLLMEmbedder

        config = LitellmEmbeddingBackend(model='openai/text-embedding-3-small')
        model = await get_embedding_model(config)
        assert isinstance(model, LiteLLMEmbedder)

    @pytest.mark.asyncio
    async def test_get_embedding_model_none_defaults_to_onnx(self) -> None:
        from memex_core.memory.models.embedding import get_embedding_model, FastEmbedder

        with (
            patch('memex_core.memory.models.base.BaseOnnxModel.__init__', return_value=None),
            patch('pathlib.Path.exists', return_value=True),
        ):
            model = await get_embedding_model(None)
        assert isinstance(model, FastEmbedder)

    @pytest.mark.asyncio
    async def test_get_reranking_model_onnx(self) -> None:
        from memex_core.memory.models.reranking import get_reranking_model, FastReranker

        with (
            patch('memex_core.memory.models.base.BaseOnnxModel.__init__', return_value=None),
            patch('pathlib.Path.exists', return_value=True),
        ):
            model = await get_reranking_model(OnnxBackend())
        assert isinstance(model, FastReranker)

    @pytest.mark.asyncio
    async def test_get_reranking_model_litellm(self) -> None:
        from memex_core.memory.models.reranking import get_reranking_model
        from memex_core.memory.models.backends.litellm_reranker import LiteLLMReranker

        config = LitellmRerankerBackend(model='cohere/rerank-v3.5')
        model = await get_reranking_model(config)
        assert isinstance(model, LiteLLMReranker)

    @pytest.mark.asyncio
    async def test_get_reranking_model_disabled(self) -> None:
        from memex_core.memory.models.reranking import get_reranking_model

        model = await get_reranking_model(DisabledBackend())
        assert model is None
