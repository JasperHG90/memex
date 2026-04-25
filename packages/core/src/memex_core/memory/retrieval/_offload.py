"""Module-level semaphores gating sync-offload model calls.

One semaphore per *model class* (reranker / embedding / NER), shared across all
asyncio.to_thread call sites that hit the same model. Sharing the cap is
deliberate: one model has one capacity budget; gating each site separately
would over-count effective parallelism (cap=2 per site -> 4 in flight against
a single model, exhausting its memory budget). See RFC-001 §"Step 1.5.2"
(AC-010 rev 2) and AC-009.

Initialised once at server startup via ``configure_offload_semaphores(cfg)``,
which is called from ``server/__init__.py`` *before* the warmup block so
warmup acquires through the production gate (W1 — see RFC-001 §"Step 1.5.4").

Note: the underlying thread keeps running after ``asyncio.wait_for`` fires;
the cap is what prevents thread accumulation, not the timeout.
"""

from __future__ import annotations

import asyncio

from memex_common.config import ServerConfig

# Gates memory/retrieval/document_search.py:243 + memory/retrieval/engine.py:1086
_RERANKER_SEMAPHORE: asyncio.Semaphore | None = None

# Gates api.py:1287 + memory/retrieval/document_search.py:130 + memory/retrieval/engine.py:208
_EMBEDDING_SEMAPHORE: asyncio.Semaphore | None = None

# Gates memory/retrieval/engine.py:322
_NER_SEMAPHORE: asyncio.Semaphore | None = None

_CFG: ServerConfig | None = None


def configure_offload_semaphores(cfg: ServerConfig) -> None:
    """Initialise the three module-level semaphores from ServerConfig.

    Must be called before any gated to_thread site fires. In production this is
    invoked at server startup (``server/__init__.py``) ahead of the model
    warmup block, so warmup itself acquires through the production gate.

    Tests may call this with a small-cap config to drive concurrency assertions
    without monkeypatching globals; per-test reconfiguration is supported.
    """
    global _RERANKER_SEMAPHORE, _EMBEDDING_SEMAPHORE, _NER_SEMAPHORE, _CFG
    _RERANKER_SEMAPHORE = asyncio.Semaphore(cfg.reranker_max_concurrency)
    _EMBEDDING_SEMAPHORE = asyncio.Semaphore(cfg.embedding_max_concurrency)
    _NER_SEMAPHORE = asyncio.Semaphore(cfg.ner_max_concurrency)
    _CFG = cfg


def _require_configured() -> ServerConfig:
    if _CFG is None:
        raise RuntimeError(
            'configure_offload_semaphores(cfg) must be called before any gated '
            'sync-offload site fires. In production this happens at server '
            'startup (server/__init__.py); tests must call it explicitly.'
        )
    return _CFG


def get_reranker_semaphore() -> asyncio.Semaphore:
    """Return the shared reranker semaphore. Raises if not configured."""
    if _RERANKER_SEMAPHORE is None:
        _require_configured()
    assert _RERANKER_SEMAPHORE is not None  # narrowed by _require_configured
    return _RERANKER_SEMAPHORE


def get_embedding_semaphore() -> asyncio.Semaphore:
    """Return the shared embedding semaphore. Raises if not configured."""
    if _EMBEDDING_SEMAPHORE is None:
        _require_configured()
    assert _EMBEDDING_SEMAPHORE is not None
    return _EMBEDDING_SEMAPHORE


def get_ner_semaphore() -> asyncio.Semaphore:
    """Return the shared NER semaphore. Raises if not configured."""
    if _NER_SEMAPHORE is None:
        _require_configured()
    assert _NER_SEMAPHORE is not None
    return _NER_SEMAPHORE


def get_reranker_call_timeout() -> float:
    """Return the per-call reranker timeout (seconds)."""
    return float(_require_configured().reranker_call_timeout)


def get_embedding_call_timeout() -> float:
    """Return the per-call embedding timeout (seconds)."""
    return float(_require_configured().embedding_call_timeout)


def get_ner_call_timeout() -> float:
    """Return the per-call NER timeout (seconds)."""
    return float(_require_configured().ner_call_timeout)
