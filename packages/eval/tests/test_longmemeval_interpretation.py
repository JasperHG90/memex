"""Tests for the retrieval containment interpretation matrix."""

from __future__ import annotations

from memex_eval.external.longmemeval_judge import compute_interpretation


class TestInterpretationMatrix:
    """All 5 outcomes of the 2x2 matrix (retrieval_contains x answer_correct)."""

    def test_correct(self) -> None:
        """Retrieval contains answer AND hypothesis correct -> correct."""
        assert (
            compute_interpretation(
                retrieval_contains=True,
                answer_correct=True,
                is_abstention_hypothesis=False,
            )
            == 'correct'
        )

    def test_model_error(self) -> None:
        """Retrieval contains answer AND hypothesis wrong -> model_error."""
        assert (
            compute_interpretation(
                retrieval_contains=True,
                answer_correct=False,
                is_abstention_hypothesis=False,
            )
            == 'model_error'
        )

    def test_correct_abstention(self) -> None:
        """Retrieval does NOT contain answer AND agent abstained -> correct_abstention."""
        assert (
            compute_interpretation(
                retrieval_contains=False,
                answer_correct=False,
                is_abstention_hypothesis=True,
            )
            == 'correct_abstention'
        )

    def test_hallucination(self) -> None:
        """Retrieval does NOT contain answer AND agent answered wrong -> hallucination."""
        assert (
            compute_interpretation(
                retrieval_contains=False,
                answer_correct=False,
                is_abstention_hypothesis=False,
            )
            == 'hallucination'
        )

    def test_lucky_guess(self) -> None:
        """Retrieval does NOT contain answer AND agent answered correctly -> lucky_guess."""
        assert (
            compute_interpretation(
                retrieval_contains=False,
                answer_correct=True,
                is_abstention_hypothesis=False,
            )
            == 'lucky_guess'
        )

    def test_correct_abstention_when_also_correct(self) -> None:
        """Edge case: retrieval missing, answer marked correct, AND abstention.

        This occurs for abstention questions where the agent correctly
        abstained and retrieval didn't contain the answer. The abstention
        check fires first: correct_abstention.
        """
        result = compute_interpretation(
            retrieval_contains=False,
            answer_correct=True,
            is_abstention_hypothesis=True,
        )
        assert result == 'correct_abstention'
