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
    """

    @property
    def model_version(self) -> str: ...

    def score(self, query: str, texts: list[str]) -> Any: ...
