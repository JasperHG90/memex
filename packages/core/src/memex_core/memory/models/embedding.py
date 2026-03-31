from __future__ import annotations

import logging
from typing import cast

import httpx
import numpy as np
from memex_common.config import EmbeddingBackend
from memex_core.memory.models.base import (
    BaseOnnxModel,
    ModelDownloader,
    MODEL_REGISTRY,
    get_cache_dir,
)
from memex_core.memory.models.protocols import EmbeddingsModel

logger = logging.getLogger('memex.core.memory.models.embedding')

# Module-level cache: avoids reloading ONNX sessions across FastAPI lifespan
# restarts (e.g. in test suites that create a new TestClient per test).
_onnx_embedder_cache: FastEmbedder | None = None


async def get_embedding_model(
    config: EmbeddingBackend | None = None,
    batch_size: int = 0,
) -> EmbeddingsModel:
    """Create an embedding model from config.

    Args:
        config: Backend configuration.  ``None`` or ``OnnxBackend`` uses the
            built-in ONNX model.  ``LitellmEmbeddingBackend`` delegates to
            the litellm-backed adapter.
        batch_size: Max texts per ONNX inference call. 0 = unbounded.

    Returns:
        An object satisfying the ``EmbeddingsModel`` protocol.
    """
    global _onnx_embedder_cache
    from memex_common.config import OnnxBackend, LitellmEmbeddingBackend

    if config is None or isinstance(config, OnnxBackend):
        if _onnx_embedder_cache is not None:
            _onnx_embedder_cache.batch_size = batch_size
            return _onnx_embedder_cache

        _spec = MODEL_REGISTRY['embedding']
        path = get_cache_dir() / _spec.repo_id.replace('/', '__') / _spec.revision

        if not path.exists():
            logger.warning(
                'Embedding model not found at %s. Downloading from Hugging Face Hub...', path
            )
            downloader = ModelDownloader(repo_id=_spec.repo_id, revision=_spec.revision)
            await downloader.download_async(httpx.AsyncClient(), force=False)

        _onnx_embedder_cache = FastEmbedder(
            model_dir=str(path), model_name='model.onnx', batch_size=batch_size
        )
        return _onnx_embedder_cache

    if isinstance(config, LitellmEmbeddingBackend):
        from memex_core.memory.models.backends.litellm_embedder import LiteLLMEmbedder

        return LiteLLMEmbedder(config)

    raise ValueError(f'Unknown embedding backend: {type(config)}')


class FastEmbedder(BaseOnnxModel):
    def __init__(
        self,
        model_dir: str,
        model_name: str = 'model.onnx',
        batch_size: int = 0,
    ) -> None:
        super().__init__(model_dir=model_dir, model_name=model_name)
        self.batch_size = batch_size

    def encode(self, text: list[str]) -> np.ndarray[tuple[int, int], np.dtype[np.float32]]:
        """Retrieve the embedding for a text query.

        Args:
            text (list[str]): Input texts to be embedded.

        Returns:
            np.ndarray: Array containing the embedding for the input text.
        """
        if not text:
            return np.empty((0, 0), dtype=np.float32)

        chunk_size = self.batch_size if self.batch_size > 0 else len(text)
        all_embeddings: list[np.ndarray] = []

        for i in range(0, len(text), chunk_size):
            batch = text[i : i + chunk_size]

            input_ids = []
            attention_mask = []
            for e in self.tokenizer.encode_batch(batch):
                input_ids.append(np.array(e.ids, dtype=np.int64))
                attention_mask.append(np.array(e.attention_mask, dtype=np.int64))

            inputs = {
                'input_ids': np.vstack(input_ids),
                'attention_mask': np.vstack(attention_mask),
            }

            outputs = cast(list[np.ndarray], self.session.run(None, inputs))
            all_embeddings.append(outputs[0])

        return np.vstack(all_embeddings)
