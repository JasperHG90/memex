"""LiteLLM-backed reranker adapter.

Wraps ``litellm.rerank`` to satisfy the ``RerankerModel`` protocol,
allowing any litellm-supported provider (Cohere, Together AI, Voyage, …)
to be used as the reranking backend.
"""

import logging
import math

import litellm
import numpy as np

from memex_common.config import LitellmRerankerBackend

logger = logging.getLogger('memex.core.memory.models.backends.litellm_reranker')


class LiteLLMReranker:
    """Reranker adapter backed by litellm.

    Satisfies ``RerankerModel`` protocol via structural subtyping.
    ``score()`` is synchronous — the retrieval engine already runs it
    inside ``asyncio.to_thread`` (see ``retrieval/engine.py:984``).

    Score semantics
    ~~~~~~~~~~~~~~~
    The retrieval engine applies sigmoid normalisation to raw scores
    (``retrieval/engine.py:987``).  The built-in ONNX model outputs raw
    logits, so the sigmoid produces correct [0, 1] probabilities.

    LiteLLM providers return ``relevance_score`` already in [0, 1].
    To keep the retrieval engine code unchanged we apply the **inverse
    sigmoid (logit)** here, so the pipeline's sigmoid recovers the
    original provider scores.
    """

    def __init__(self, config: LitellmRerankerBackend) -> None:
        self._model = config.model
        self._api_base = str(config.api_base) if config.api_base else None
        self._api_key = config.api_key.get_secret_value() if config.api_key else None
        logger.info(
            'LiteLLM reranker initialised: model=%s api_base=%s',
            self._model,
            self._api_base,
        )

    def score(self, query: str, texts: list[str]) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
        """Score query-document pairs.

        Returns:
            ``np.ndarray`` of shape ``(len(texts),)`` — logit-transformed
            scores in the **original document order**.
        """
        if not texts:
            raise ValueError('Empty text list provided for reranking.')

        response = litellm.rerank(
            model=self._model,
            query=query,
            documents=texts,
            return_documents=False,
            api_base=self._api_base,
            api_key=self._api_key,
        )

        # litellm returns results sorted by relevance_score descending;
        # restore original document order.
        score_by_index: dict[int, float] = {
            r['index']: r['relevance_score'] for r in response.results
        }
        ordered_scores = [score_by_index[i] for i in range(len(texts))]

        # Apply inverse sigmoid (logit) so the retrieval engine's
        # sigmoid normalisation recovers the original [0, 1] scores.
        logit_scores: list[float] = []
        for s in ordered_scores:
            clamped = max(1e-7, min(1 - 1e-7, s))
            logit_scores.append(math.log(clamped / (1 - clamped)))

        return np.array(logit_scores, dtype=np.float32)
