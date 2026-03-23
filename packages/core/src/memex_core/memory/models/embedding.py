import logging
import pathlib as plb
from typing import cast

import httpx
import numpy as np
from platformdirs import user_cache_dir
from async_lru import alru_cache
from memex_core.memory.models.base import BaseOnnxModel, ModelDownloader, MODEL_REGISTRY

logger = logging.getLogger('memex.core.memory.models.embedding')


@alru_cache(maxsize=1)
async def get_embedding_model() -> 'FastEmbedder':
    """Get the embedding model

    Args:
        model_dir (str | plb.Path | None, optional): Location of the model directory. Defaults to None.
        model_name (str, optional): Name of the model file. Defaults to 'model.onnx'.

    Returns:
        FastEmbedder: Embedding model instance.
    """
    _spec = MODEL_REGISTRY['embedding']
    path = plb.Path(user_cache_dir('memex')) / _spec.repo_id.replace('/', '__') / _spec.revision

    if not path.exists():
        logger.warning(f'Embedding model not found at {path}. Downloading from Hugging Face Hub...')
        downloader = ModelDownloader(repo_id=_spec.repo_id, revision=_spec.revision)
        await downloader.download_async(httpx.AsyncClient(), force=False)

    return FastEmbedder(model_dir=str(path), model_name='model.onnx')


class FastEmbedder(BaseOnnxModel):
    def encode(self, text: list[str]) -> np.ndarray[tuple[int, int], np.dtype[np.float32]]:
        """Retrieve the embedding for a text query.

        Args:
            text (list[str]): Input texts to be embedded.

        Returns:
            np.ndarray: Array containing the embedding for the input text.
        """
        input_ids = []
        attention_mask = []
        for e in self.tokenizer.encode_batch(text):
            input_ids.append(np.array(e.ids, dtype=np.int64))
            attention_mask.append(np.array(e.attention_mask, dtype=np.int64))

        inputs = {
            'input_ids': np.vstack(input_ids),
            'attention_mask': np.vstack(attention_mask),
        }

        outputs = cast(list[np.ndarray], self.session.run(None, inputs))

        return outputs[0]
