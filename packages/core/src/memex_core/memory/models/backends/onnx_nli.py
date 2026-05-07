"""ONNX-backed NLI (Natural Language Inference) classifier.

Wraps a cross-encoder NLI model (default ``cross-encoder/nli-deberta-v3-xsmall``)
with the same ``BaseOnnxModel`` substrate that ``FastReranker`` / ``FastEmbedder``
use, so the model lifecycle (download, ONNX session, tokenizer) is shared.

The model emits three logits per ``(premise, hypothesis)`` pair which we softmax
into ``{entailment, neutral, contradiction}`` probabilities.

This is NOT the ContradictionEngine. The NLI classifier is a fast 3-way gate
used in the surprise pipeline's polarity branch — it detects contradiction
probability as a cheap pre-filter before the full ContradictionEngine (which
uses DSPy signatures for structured contradiction analysis) runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import cast

import numpy as np

from memex_core.memory.models.base import BaseOnnxModel

logger = logging.getLogger('memex.core.memory.models.backends.onnx_nli')


_LABEL_ORDER: tuple[str, str, str] = ('contradiction', 'entailment', 'neutral')


def _load_label_order_from_config(model_dir: str) -> tuple[str, str, str] | None:
    """Read ``config.json`` and return the model's logit-index → label tuple.

    Returns ``None`` when the file is missing or malformed; the caller treats
    that as "no validation possible" and proceeds with the supplied order.
    HuggingFace transformer configs use string keys (``"0"``, ``"1"``, ``"2"``)
    in ``id2label``; we normalise to ``int`` before sorting.
    """
    import pathlib

    config_path = pathlib.Path(model_dir) / 'config.json'
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    id2label = config.get('id2label')
    if not isinstance(id2label, dict):
        return None
    try:
        ordered = sorted(
            ((int(k), str(v).lower()) for k, v in id2label.items()), key=lambda x: x[0]
        )
    except (TypeError, ValueError):
        return None
    if len(ordered) != 3 or [i for i, _ in ordered] != [0, 1, 2]:
        return None
    return cast(tuple[str, str, str], tuple(label for _, label in ordered))


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
        config_label_order = _load_label_order_from_config(model_dir)
        effective = label_order or config_label_order or _LABEL_ORDER
        if (
            label_order is not None
            and config_label_order is not None
            and tuple(label_order) != tuple(config_label_order)
        ):
            raise ValueError(
                f'NLI label_order {label_order} does not match the model '
                f'config.json id2label order {config_label_order}. Logits would '
                'be silently misattributed.'
            )
        if (
            label_order is None
            and config_label_order is not None
            and tuple(config_label_order) != tuple(_LABEL_ORDER)
        ):
            raise ValueError(
                f'NLI model config.json declares id2label={config_label_order} '
                f'but the default expects {_LABEL_ORDER}. Pass an explicit '
                "label_order=... to opt in to the model's ordering."
            )
        self._label_order = effective
        # ONNX Runtime documents `Run()` as thread-safe on a shared session,
        # but `_score_pair` is invoked via `asyncio.to_thread` and the
        # scheduler can drive multiple vault ticks; serialise inference with
        # a `threading.Lock` so the path is safe under any provider/runtime
        # where that guarantee weakens (e.g. some EP combinations).
        self._inference_lock = threading.Lock()
        logger.info(
            'OnnxNLIClassifier initialised (model_version=%s, label_order=%s, config_validated=%s)',
            model_version,
            effective,
            config_label_order is not None,
        )

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

        with self._inference_lock:
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
        """Classify a single (premise, hypothesis) pair.

        Concurrency notes for the to_thread audit:
          - Single call site: ``LintLLMService.maybe_run`` (services/lint_llm.py).
          - That call site is already throttled twice over:
            (a) per-vault scheduler tick processes ``units_per_tick`` serially;
            (b) the surprise-gated polarity branch only fires when cosine
            surprise < ``surprise_threshold``.
          - The ``threading.Lock`` in ``_score_pair`` is for ONNX-runtime
            safety on ``session.run``; tokenisation and softmax run free.
            It does NOT cap concurrency end-to-end.
        """
        # exempt: caller-side throttled (LintLLMService.maybe_run); a shared
        # async semaphore here would be redundant. See classify() docstring.
        return await asyncio.to_thread(self._score_pair, premise, hypothesis)
