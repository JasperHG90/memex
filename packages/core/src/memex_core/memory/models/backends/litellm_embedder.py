"""LiteLLM-backed embedding adapter.

Wraps ``litellm.embedding`` to satisfy the ``EmbeddingsModel`` protocol,
allowing any litellm-supported provider (OpenAI, Google, Cohere, Ollama, …)
to be used as the embedding backend.
"""

import logging
from typing import Any

import litellm
import numpy as np

from memex_common.config import LitellmEmbeddingBackend

logger = logging.getLogger('memex.core.memory.models.backends.litellm_embedder')


class LiteLLMEmbedder:
    """Embedding adapter backed by litellm.

    Satisfies ``EmbeddingsModel`` protocol via structural subtyping.
    ``encode()`` is synchronous — callers already run it inside
    ``asyncio.to_thread`` (see ``retrieval/engine.py``, ``embedding_processor.py``).
    """

    def __init__(self, config: LitellmEmbeddingBackend) -> None:
        self._model = config.model
        self._api_base = str(config.api_base) if config.api_base else None
        self._api_key = config.api_key.get_secret_value() if config.api_key else None
        self._dimensions = config.dimensions
        logger.info(
            'LiteLLM embedder initialised: model=%s api_base=%s dimensions=%s',
            self._model,
            self._api_base,
            self._dimensions,
        )

    def encode(self, text: list[str]) -> np.ndarray[tuple[int, int], np.dtype[np.float32]]:
        """Encode texts into embedding vectors.

        Returns:
            ``np.ndarray`` of shape ``(len(text), dim)`` with ``float32`` dtype —
            same contract as ``FastEmbedder.encode``.
        """
        kwargs: dict[str, Any] = {}
        if self._api_base:
            kwargs['api_base'] = self._api_base
        if self._api_key:
            kwargs['api_key'] = self._api_key
        if self._dimensions:
            kwargs['dimensions'] = self._dimensions

        response = litellm.embedding(model=self._model, input=text, **kwargs)

        # response.data is list[Embedding] where each has .embedding (list[float])
        vectors = [item['embedding'] for item in response.data]
        return np.array(vectors, dtype=np.float32)
