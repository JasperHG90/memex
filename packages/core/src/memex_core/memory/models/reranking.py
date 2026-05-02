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

# Module-level cache: avoids reloading ONNX sessions across FastAPI lifespan
# restarts (e.g. in test suites that create a new TestClient per test).
_onnx_reranker_cache: 'FastReranker | None' = None


async def get_reranking_model(
    config: RerankerBackend | None = None,
    batch_size: int = 0,
) -> RerankerModel | None:
    """Create a reranking model from config.

    Args:
        config: Backend configuration.  ``None`` or ``OnnxBackend`` uses the
            built-in ONNX model.  ``LitellmRerankerBackend`` delegates to
            the litellm-backed adapter.  ``DisabledBackend`` returns ``None``.

            ``OnnxBackend.quantization='int8'`` (F42) lazily produces an int8
            variant of the cross-encoder weights on first load (cached on
            disk) for ~2× CPU speedup. The variant is folded into
            ``FastReranker.model_version`` so the F41 score cache invalidates
            cleanly when the field is flipped.

    Returns:
        An object satisfying the ``RerankerModel`` protocol, or ``None``
        if reranking is disabled.
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

        # F42: opt-in int8 dynamic quantization. Default ``fp32`` short-circuits
        # to the stock model.  ``int8`` lazily produces ``model.int8.onnx`` on
        # first call (cached on disk) and threads the variant tag into
        # ``model_version`` so the F41 cache invalidates structurally on flip.
        variant: str = getattr(config, 'quantization', 'fp32') if config is not None else 'fp32'
        if variant == 'int8':
            from memex_core.memory.models.quantization import ensure_quantized_model

            model_filename = ensure_quantized_model(path, 'model.onnx')
        else:
            model_filename = 'model.onnx'

        _onnx_reranker_cache = FastReranker(
            model_dir=str(path),
            model_name=model_filename,
            batch_size=batch_size,
            model_version=f'onnx:{_spec.repo_id}:{_spec.revision}:{variant}',
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
            # Hermes round-1 LOW — the default sentinel disables F41
            # structural cache invalidation on model upgrades. Production
            # callers go through ``get_reranking_model`` which always
            # supplies a versioned identifier; the fallback is a footgun
            # for ad-hoc instantiation in tests/benchmarks.
            logger.warning(
                'FastReranker constructed with default model_version=%r — '
                'F41 cache will not invalidate on model upgrade until TTL '
                'expires. Pass an explicit model_version (e.g. '
                '"onnx:repo_id:revision") if you intend to swap models.',
                model_version,
            )
        self._model_version = model_version

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def variant(self) -> str:
        """F42 — quantization variant tag (``fp32`` | ``int8`` | ``unknown``).

        Parsed from the ``model_version`` trailing segment to label the
        ``RERANKER_LATENCY_SECONDS`` histogram. Falls back to ``unknown``
        for legacy ``onnx:repo:rev`` tags (no variant suffix) or anything
        ad-hoc.
        """
        parts = self._model_version.rsplit(':', 1)
        if len(parts) == 2 and parts[1] in ('fp32', 'int8'):
            return parts[1]
        return 'unknown'

    def score(
        self,
        query: str,
        texts: list[str],
    ) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
        """Rerank a list of texts based on the query.

        Args:
            query (str): The search query.
            texts (List[str]): List of document texts to score.

        Returns:
            np.ndarray: Column vector of scores corresponding to the texts.
            texts are scored in the order provided.
        """
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
        """
        Sorts texts and IDs based on raw scores (descending).

        Args:
            query: The search query.
            texts: List of document texts to score.
            doc_ids: Optional list of identifiers. If None, 0-based indices are used.

        Returns:
            A list of dicts: [{'id': ..., 'score': ..., 'text': ...}, ...] sorted by score.
        """
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
