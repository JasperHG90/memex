"""ONNX-backed NLI classifier (F10b).

Wraps a cross-encoder NLI model (default ``cross-encoder/nli-deberta-v3-small``)
with the same ``BaseOnnxModel`` substrate that ``FastReranker`` / ``FastEmbedder``
use, so the model lifecycle (download, ONNX session, tokenizer) is shared.

The model emits three logits per ``(premise, hypothesis)`` pair which we softmax
into ``{entailment, neutral, contradiction}`` probabilities.
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import numpy as np

from memex_core.memory.models.base import BaseOnnxModel

logger = logging.getLogger('memex.core.memory.models.backends.onnx_nli')


_LABEL_ORDER: tuple[str, str, str] = ('contradiction', 'entailment', 'neutral')


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max()
    exp = np.exp(shifted)
    return exp / exp.sum()


class OnnxNLIClassifier(BaseOnnxModel):
    """Three-way NLI classifier satisfying :class:`NLIClassifierModel`.

    Synchronous ``_score_pair`` is wrapped by an async ``classify`` so callers
    do not block the event loop on inference (matches the protocol contract;
    the lint_llm gate already runs on the scheduler thread).
    """

    def __init__(
        self,
        model_dir: str,
        model_name: str = 'model.onnx',
        model_version: str = 'onnx:unknown',
        label_order: tuple[str, str, str] | None = None,
    ) -> None:
        super().__init__(model_dir=model_dir, model_name=model_name)
        self._model_version = model_version
        self._label_order = label_order or _LABEL_ORDER

    @property
    def model_version(self) -> str:
        return self._model_version

    def _score_pair(self, premise: str, hypothesis: str) -> dict[str, float]:
        encoding = self.tokenizer.encode(premise, hypothesis)
        input_ids = np.array([encoding.ids], dtype=np.int64)
        attention_mask = np.array([encoding.attention_mask], dtype=np.int64)

        inputs: dict[str, np.ndarray] = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
        }
        expected = {i.name for i in self.session.get_inputs()}
        if 'token_type_ids' in expected:
            inputs['token_type_ids'] = np.array([encoding.type_ids], dtype=np.int64)

        outputs = cast(list[np.ndarray], self.session.run(None, inputs))
        logits = outputs[0].flatten().astype(np.float32)
        if logits.shape[0] != 3:
            raise ValueError(
                f'NLI model produced {logits.shape[0]} logits, expected 3 '
                '(contradiction / entailment / neutral).'
            )
        probs = _softmax(logits)
        return {label: float(probs[i]) for i, label in enumerate(self._label_order)}

    async def classify(self, premise: str, hypothesis: str) -> dict[str, float]:
        return await asyncio.to_thread(self._score_pair, premise, hypothesis)
