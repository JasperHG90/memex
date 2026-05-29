"""Suite-private outcome for ``inbox_router``: routing top-1 accuracy."""

from __future__ import annotations

from typing import Any, Literal

from memex_eval.suite.base import ExpectedOutcomeBase, register_outcome


@register_outcome('inbox_route_accuracy')
class InboxRouteAccuracy(ExpectedOutcomeBase):
    """Score the router's top-1 routing accuracy against ground-truth labels.

    Reads the predictions + expected mapping the ``seed_inbox_router_corpus``
    setup action published into the scenario context (auto-prefixed with the
    handler name). For each labelled inbox note, a hit is the router's top
    candidate matching the expected vault. ``pass`` when accuracy ≥ ``min_accuracy``.
    """

    type: Literal['inbox_route_accuracy']
    min_accuracy: float = 0.75
    predictions_key: str = 'seed_inbox_router_corpus.predictions'
    expected_key: str = 'seed_inbox_router_corpus.expected'

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'accuracy', 'n_notes', 'n_correct']

    def score(
        self,
        answer: Any,
        scenario: Any,
        *,
        context: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> dict[str, float]:
        ctx = context or {}
        predictions: dict[str, Any] = ctx.get(self.predictions_key) or {}
        expected: dict[str, Any] = ctx.get(self.expected_key) or {}

        total = len(expected)
        if total == 0:
            # No labelled notes resolved — treat as an error, not a silent pass.
            return {'pass': 0.0, 'accuracy': 0.0, 'n_notes': 0.0, 'n_correct': 0.0}

        correct = sum(
            1 for note_id, exp_vault in expected.items() if predictions.get(note_id) == exp_vault
        )
        accuracy = correct / total
        return {
            'pass': 1.0 if accuracy >= self.min_accuracy else 0.0,
            'accuracy': accuracy,
            'n_notes': float(total),
            'n_correct': float(correct),
        }
