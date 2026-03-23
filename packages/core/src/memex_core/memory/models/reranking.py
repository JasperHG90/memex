import logging
import pathlib as plb
from typing import cast

import httpx
import numpy as np

from async_lru import alru_cache
from memex_core.memory.models.base import BaseOnnxModel, ModelDownloader, MODEL_REGISTRY
from platformdirs import user_cache_dir

logger = logging.getLogger('memex.core.memory.models.reranking')


@alru_cache(maxsize=1)
async def get_reranking_model() -> 'FastReranker':
    """Get the reranking model.

    Returns:
        FastReranker: Reranking model instance.
    """
    _spec = MODEL_REGISTRY['reranker']
    path = plb.Path(user_cache_dir('memex')) / _spec.repo_id.replace('/', '__') / _spec.revision

    if not path.exists():
        logger.warning(f'Reranking model not found at {path}. Downloading from Hugging Face Hub...')
        downloader = ModelDownloader(repo_id=_spec.repo_id, revision=_spec.revision)
        await downloader.download_async(client=httpx.AsyncClient(), force=False)

    return FastReranker(model_dir=str(path), model_name='model.onnx')


class FastReranker(BaseOnnxModel):
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

        # The tokenizer handles the [CLS] Q [SEP] D [SEP] construction
        encodings = self.tokenizer.encode_batch(pairs)

        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.array([e.type_ids for e in encodings], dtype=np.int64)

        inputs = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'token_type_ids': token_type_ids,
        }

        outputs = cast(list[np.ndarray], self.session.run(None, inputs))

        return outputs[0].flatten()

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
