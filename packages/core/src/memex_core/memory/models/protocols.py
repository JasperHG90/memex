"""Protocols for inference model backends (embedding, reranking).

These define the contracts that all backends must satisfy.
Both the built-in ONNX models and litellm-based adapters
implement these protocols via structural subtyping.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingsModel(Protocol):
    """Any backend that encodes text into embedding vectors.

    Implementations must accept a list of strings and return
    a 2-D array-like of shape ``(len(text), embedding_dim)``.
    """

    def encode(self, text: list[str]) -> Any: ...


@runtime_checkable
class RerankerModel(Protocol):
    """Any backend that scores query-document relevance.

    ``score()`` accepts a query and a list of document texts
    and returns a 1-D array-like of float scores in input order.

    ``model_version`` is a stable string identifier (provider:model:revision
    style) that distinguishes one cross-encoder weight set from another.
    The retrieval-side score cache (F41) keys on this so a model upgrade
    invalidates entries structurally rather than waiting for the TTL.

    Implementations SHOULD return a stable identifier that changes when the
    underlying model is upgraded — e.g. ``'onnx:<repo_id>:<revision>'`` or
    ``'litellm:<model_id>'`` — so the F41 score cache can invalidate entries
    structurally on a swap rather than waiting on the TTL.

    **Backwards compatibility note (F41).** ``Protocol`` is structural, so the
    body of ``model_version`` below is *not* what makes pre-F41 backends
    satisfy this contract — Python never calls it. The actual runtime
    backwards-compat mechanism is the ``getattr(reranker, 'model_version',
    'unknown')`` guard on the cache call site (``retrieval/engine.py`` —
    ``_reranker_score``). Out-of-tree backends that simply lack the attribute
    fall back to the literal ``'unknown'`` constant. The default value here
    documents that fallback in one place; if it ever returned a real value,
    you'd disable structural cache invalidation across the fleet on a model
    upgrade and have to wait for the 24h TTL to flush stale entries.
    """

    @property
    def model_version(self) -> str:
        return 'unknown'

    def score(self, query: str, texts: list[str]) -> Any: ...
