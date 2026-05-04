from __future__ import annotations

import logging
from typing import cast

import httpx
import numpy as np

from memex_common.config import RerankerBackend
from memex_core.memory.models.base import (
    BaseOnnxModel,
    ModelDownloader,
    MODEL_REGISTRY,
    get_cache_dir,
)
from memex_core.memory.models.protocols import RerankerModel

logger = logging.getLogger('memex.core.memory.models.reranking')

# Module-level cache: avoids reloading ONNX sessions across FastAPI lifespan restarts.
_onnx_reranker_cache: 'FastReranker | None' = None


async def get_reranking_model(
    config: RerankerBackend | None = None,
    batch_size: int = 0,
) -> RerankerModel | None:
    """Create a reranking model from config.

    None/OnnxBackend -> built-in ONNX. LitellmRerankerBackend -> litellm adapter.
    DisabledBackend -> None.
    """
    global _onnx_reranker_cache
    from memex_common.config import OnnxBackend, LitellmRerankerBackend, DisabledBackend

    if config is None or isinstance(config, OnnxBackend):
        if _onnx_reranker_cache is not None:
            return _onnx_reranker_cache

        _spec = MODEL_REGISTRY['reranker']
        path = get_cache_dir() / _spec.repo_id.replace('/', '__') / _spec.revision

        if not path.exists():
            logger.warning(
                'Reranking model not found at %s. Downloading from Hugging Face Hub...', path
            )
            downloader = ModelDownloader(repo_id=_spec.repo_id, revision=_spec.revision)
            await downloader.download_async(client=httpx.AsyncClient(), force=False)

        _onnx_reranker_cache = FastReranker(
            model_dir=str(path),
            model_name='model.onnx',
            batch_size=batch_size,
            model_version=f'onnx:{_spec.repo_id}:{_spec.revision}',
        )
        return _onnx_reranker_cache

    if isinstance(config, LitellmRerankerBackend):
        from memex_core.memory.models.backends.litellm_reranker import LiteLLMReranker

        return LiteLLMReranker(config)

    if isinstance(config, DisabledBackend):
        return None

    raise ValueError(f'Unknown reranker backend: {type(config)}')


class FastReranker(BaseOnnxModel):
    def __init__(
        self,
        model_dir: str,
        model_name: str = 'model.onnx',
        batch_size: int = 0,
        model_version: str = 'onnx:unknown',
    ) -> None:
        super().__init__(model_dir=model_dir, model_name=model_name)
        self.batch_size = batch_size
        if model_version == 'onnx:unknown':
            # Default sentinel disables structural cache invalidation on model
            # upgrades. Production callers always supply a versioned identifier.
            logger.warning(
                'FastReranker constructed with default model_version=%r — '
                'cache will not invalidate on model upgrade until TTL '
                'expires. Pass an explicit model_version (e.g. '
                '"onnx:repo_id:revision") if you intend to swap models.',
                model_version,
            )
        self._model_version = model_version

    @property
    def model_version(self) -> str:
        return self._model_version

    def score(
        self,
        query: str,
        texts: list[str],
    ) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
        """Score texts against query. Returns column vector of scores in input order."""
        if not texts:
            raise ValueError('Empty text list provided for reranking.')

        # We pair the query with every text: [(Q, T1), (Q, T2), ...]
        pairs = list(zip([query] * len(texts), texts))

        chunk_size = self.batch_size if self.batch_size > 0 else len(pairs)
        all_scores: list[np.ndarray] = []

        for i in range(0, len(pairs), chunk_size):
            batch = pairs[i : i + chunk_size]

            # The tokenizer handles the [CLS] Q [SEP] D [SEP] construction
            encodings = self.tokenizer.encode_batch(batch)

            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)

            inputs = {
                'input_ids': input_ids,
                'attention_mask': attention_mask,
                'token_type_ids': token_type_ids,
            }

            outputs = cast(list[np.ndarray], self.session.run(None, inputs))
            all_scores.append(outputs[0].flatten())

        return np.concatenate(all_scores)

    def rerank(
        self, query: str, texts: list[str], doc_ids: list[str]
    ) -> list[dict[str, str | float]]:
        """Sort texts and IDs by raw scores (descending). Returns [{id, score, text}, ...]."""
        scores = self.score(query, texts)

        scores_list = scores.flatten().tolist()

        N = scores.shape[0]

        if len(doc_ids) != N:
            raise ValueError(f'Length mismatch: {len(doc_ids)} ids but {N} scores')

        zipped = zip(scores_list, doc_ids)
        sorted_data = sorted(zipped, key=lambda x: x[0], reverse=True)

        return [
            {'id': item[1], 'score': float(item[0]), 'text': texts[i]}
            for i, item in enumerate(sorted_data)
        ]
