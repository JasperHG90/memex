"""NLI classifier factory.

Mirrors :func:`memex_core.memory.models.reranking.get_reranking_model`: a config-
dispatched factory that returns an object satisfying the
:class:`NLIClassifierModel` protocol. Supports ONNX and litellm backends.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from memex_common.config import NLIModelConfig
from memex_core.memory.models.backends.onnx_nli import OnnxNLIClassifier
from memex_core.memory.models.base import (
    MODEL_REGISTRY,
    ModelDownloader,
    get_cache_dir,
)
from memex_core.memory.models.protocols import NLIClassifierModel

logger = logging.getLogger('memex.core.memory.models.nli')

_onnx_nli_cache: 'OnnxNLIClassifier | None' = None
_onnx_nli_init_lock: 'asyncio.Lock | None' = None


def _get_init_lock() -> asyncio.Lock:
    """Lazy-initialise the cache-init Lock to avoid binding to no-loop import.

    Creating ``asyncio.Lock()`` at module load triggers a deprecation warning
    in Python 3.12+ (and may hard-error in future versions) when no event loop
    is running. Lazy creation defers the bind to the first ``await`` site.
    """
    global _onnx_nli_init_lock
    if _onnx_nli_init_lock is None:
        _onnx_nli_init_lock = asyncio.Lock()
    return _onnx_nli_init_lock


async def get_nli_model(config: NLIModelConfig | None = None) -> NLIClassifierModel | None:
    """Return an NLI classifier or ``None`` when the polarity gate is disabled.

    Reuses the same cached ONNX session across FastAPI lifespan restarts as
    the embedding/reranker models — the model is ~60 MB, single-instance.
    A module-level ``asyncio.Lock`` serialises the check-and-set so two
    concurrent callers cannot both observe ``None``, both download, and
    race on the assignment (double-init wastes disk + RAM and emits
    duplicate HuggingFace fetches).
    """
    global _onnx_nli_cache

    if config is None:
        config = NLIModelConfig()

    if not config.enabled:
        return None

    if _onnx_nli_cache is not None:
        return _onnx_nli_cache

    async with _get_init_lock():
        if _onnx_nli_cache is not None:
            return _onnx_nli_cache

        spec = MODEL_REGISTRY['nli']
        path = get_cache_dir() / spec.repo_id.replace('/', '__') / spec.revision

        if not path.exists():
            logger.warning('NLI model not found at %s. Downloading from Hugging Face Hub...', path)
            downloader = ModelDownloader(repo_id=spec.repo_id, revision=spec.revision)
            async with httpx.AsyncClient() as client:
                await downloader.download_async(client=client, force=False)

        _onnx_nli_cache = OnnxNLIClassifier(
            model_dir=str(path),
            model_name='onnx/model.onnx',
            model_version=f'onnx:{spec.repo_id}:{spec.revision}',
        )
    return _onnx_nli_cache
