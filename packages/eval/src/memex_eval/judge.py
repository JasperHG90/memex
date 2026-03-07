"""LLM-as-a-judge wrapper using dspy with Gemini."""

from __future__ import annotations

import logging
import os

import dspy

logger = logging.getLogger('memex_eval.judge')


class BinaryCorrectness(dspy.Signature):
    """Judge whether a model response correctly answers a question given the ground truth."""

    question: str = dspy.InputField(desc='The question that was asked.')
    expected_answer: str = dspy.InputField(desc='The ground truth expected answer.')
    model_response: str = dspy.InputField(desc='The model/system response to evaluate.')
    is_correct: bool = dspy.OutputField(desc='Whether the response is correct.')
    reasoning: str = dspy.OutputField(desc='Brief explanation of the judgment.')


class RetrievalRelevance(dspy.Signature):
    """Judge whether a search result is relevant to the query and matches expected content."""

    query: str = dspy.InputField(desc='The search query.')
    expected_content: str = dspy.InputField(desc='What the result should contain or convey.')
    search_result: str = dspy.InputField(desc='The top search result text.')
    is_relevant: bool = dspy.OutputField(desc='Whether the result is relevant and correct.')
    reasoning: str = dspy.OutputField(desc='Brief explanation of the judgment.')


class GradedCorrectness(dspy.Signature):
    """Judge model response correctness on a graded scale."""

    question: str = dspy.InputField(desc='The question that was asked.')
    expected_answer: str = dspy.InputField(desc='The ground truth expected answer.')
    model_response: str = dspy.InputField(desc='The model/system response to evaluate.')
    score: float = dspy.OutputField(
        desc='0.0 (wrong), 0.25 (minimal), 0.5 (partial), 0.75 (mostly correct), 1.0 (correct)'
    )
    reasoning: str = dspy.OutputField(desc='Brief explanation of the judgment.')


class Judge:
    """LLM-as-a-judge using dspy with Gemini."""

    def __init__(self, model: str | None = None, api_key: str | None = None):
        model = model or os.environ.get('EVAL_JUDGE_MODEL', 'gemini/gemini-2.5-flash')
        api_key = api_key or os.environ.get('GOOGLE_API_KEY')
        if not api_key:
            raise ValueError(
                'GOOGLE_API_KEY environment variable required for LLM judge. '
                'Set it or use --no-llm-judge to skip.'
            )
        self.lm = dspy.LM(model=model, api_key=api_key)
        self._correctness = dspy.ChainOfThought(BinaryCorrectness)
        self._relevance = dspy.ChainOfThought(RetrievalRelevance)
        self._graded = dspy.ChainOfThought(GradedCorrectness)

    def judge_correctness(self, question: str, expected: str, response: str) -> tuple[bool, str]:
        """Judge whether a response correctly answers a question.

        Returns (is_correct, reasoning).
        """
        with dspy.context(lm=self.lm):
            result = self._correctness(
                question=question,
                expected_answer=expected,
                model_response=response,
            )
        return result.is_correct, result.reasoning

    def judge_graded_correctness(
        self, question: str, expected: str, response: str
    ) -> tuple[float, str]:
        """Judge response correctness on a graded scale.

        Returns (score, reasoning) where score is in {0.0, 0.25, 0.5, 0.75, 1.0}.
        """
        with dspy.context(lm=self.lm):
            result = self._graded(
                question=question,
                expected_answer=expected,
                model_response=response,
            )
        try:
            score = float(result.score)
        except (ValueError, TypeError):
            score = 0.0
        return score, result.reasoning

    def judge_relevance(self, query: str, expected: str, search_result: str) -> tuple[bool, str]:
        """Judge whether a search result is relevant.

        Returns (is_relevant, reasoning).
        """
        with dspy.context(lm=self.lm):
            result = self._relevance(
                query=query,
                expected_content=expected,
                search_result=search_result,
            )
        return result.is_relevant, result.reasoning
