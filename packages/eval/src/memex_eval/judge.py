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


class AbstentionCorrectness(dspy.Signature):
    """Judge whether a hypothesis correctly abstains from answering.

    Used for LongMemEval ``*_abs`` questions: the ground-truth answer is
    missing/null, so correctness is defined as the hypothesis explicitly
    declining to answer (e.g. "I do not know based on the available
    memory"). Paraphrase is acceptable; hallucinating any specific answer
    is INCORRECT.
    """

    question: str = dspy.InputField(desc='The question that was asked.')
    model_response: str = dspy.InputField(desc='The model/system response to evaluate.')
    is_correct_abstention: bool = dspy.OutputField(
        desc='Whether the response correctly abstains (declines to answer).'
    )
    reasoning: str = dspy.OutputField(desc='Brief explanation of the judgment.')


class AbstentionClassifier(dspy.Signature):
    """Classify whether a hypothesis *itself* is an abstention.

    Independent of ground-truth or correctness — this is a pure labelling
    task over the hypothesis text. Used as the denominator for abstention
    precision so that metric is not definitionally coupled to correctness.
    """

    model_response: str = dspy.InputField(desc='The model/system response to classify.')
    is_abstention: bool = dspy.OutputField(
        desc='True iff the response declines to answer / says it does not know.'
    )
    reasoning: str = dspy.OutputField(desc='Brief explanation.')


class Judge:
    """LLM-as-a-judge using dspy with Gemini."""

    def __init__(self, model: str | None = None, api_key: str | None = None):
        model = model or os.environ.get('EVAL_JUDGE_MODEL', 'gemini/gemini-3-flash-preview')
        api_key = api_key or os.environ.get('GOOGLE_API_KEY')
        if not api_key:
            raise ValueError(
                'GOOGLE_API_KEY environment variable required for LLM judge. '
                'Set it or use --no-llm-judge to skip.'
            )
        # timeout= is required by the AC-006 grep guard in
        # packages/core/tests/unit/test_dspy_lm_timeout_guard.py and prevents the
        # wedge mode (issue #50): a hung LiteLLM request without a socket
        # deadline can pin the process indefinitely under memory pressure.
        self.lm = dspy.LM(model=model, api_key=api_key, timeout=120)
        # uuid of the last history entry already drained by consume_usage().
        # We track by uuid (each dspy entry has a unique one) instead of
        # absolute index because ``dspy.LM`` truncates history by popping
        # from the front once it hits ``settings.max_history_size`` (10000
        # by default) — an absolute cursor would silently misalign at
        # scale and either over- or under-count tokens.
        self._last_drained_uuid: str | None = None
        # Some models (notably gemini-3-flash-preview) return empty
        # `response.usage` through litellm even though `_hidden_params.response_cost`
        # is populated. We surface this once so a 0-token suite isn't read as
        # "judge didn't fire."
        self._warned_missing_usage: bool = False
        self._correctness = dspy.ChainOfThought(BinaryCorrectness)
        self._relevance = dspy.ChainOfThought(RetrievalRelevance)
        self._graded = dspy.ChainOfThought(GradedCorrectness)
        self._abstention_correctness = dspy.ChainOfThought(AbstentionCorrectness)
        self._abstention_classifier = dspy.ChainOfThought(AbstentionClassifier)

    def consume_usage(self) -> dict[str, float]:
        """Return cumulative {tokens_in, tokens_out, cost_usd} for every LLM
        call since the last consume_usage() and advance the cursor.

        Defensive against dspy schema drift: missing keys default to 0.
        Robust to ``dspy.LM.history`` front-truncation by tracking the
        last-seen entry uuid rather than an absolute index.
        """
        try:
            history = list(self.lm.history)
        except (AttributeError, TypeError):
            return {'tokens_in': 0.0, 'tokens_out': 0.0, 'cost_usd': 0.0}
        # Walk from the end backwards until we hit the last drained uuid;
        # everything past that is fresh. If the cursor uuid was popped off
        # the front by dspy's truncation, we drain the entire history (all
        # of which is post-cursor by definition).
        if self._last_drained_uuid is None:
            entries = history
        else:
            entries = []
            for entry in reversed(history):
                if entry.get('uuid') == self._last_drained_uuid:
                    break
                entries.append(entry)
            entries.reverse()
        if history:
            self._last_drained_uuid = history[-1].get('uuid')
        tokens_in = tokens_out = 0
        cost = 0.0
        for entry in entries:
            try:
                usage = entry.get('usage') or {}
                tokens_in += int(usage.get('prompt_tokens', 0) or 0)
                tokens_out += int(usage.get('completion_tokens', 0) or 0)
                cost += float(entry.get('cost') or 0.0)
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        # Warn only when the entire batch had cost but zero tokens — that's
        # the model genuinely failing to surface usage. Mixed batches (some
        # entries with usage, some without) net out to a populated total.
        if cost > 0 and tokens_in == 0 and tokens_out == 0 and not self._warned_missing_usage:
            logger.warning(
                'Judge model %r returned cost but empty token usage from litellm; '
                'cost.total_usd is accurate but tokens.total_in/out will be 0. '
                'Known with gemini/gemini-3-flash-preview; switch to a model '
                'with full litellm telemetry (e.g. gemini/gemini-2.5-flash).',
                getattr(self.lm, 'model', 'unknown'),
            )
            self._warned_missing_usage = True
        return {
            'tokens_in': float(tokens_in),
            'tokens_out': float(tokens_out),
            'cost_usd': cost,
        }

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

    def judge_abstention_correctness(self, question: str, response: str) -> tuple[bool, str]:
        """Judge whether a hypothesis correctly abstains.

        Used when the ground-truth answer is missing/null (LongMemEval
        ``*_abs`` questions). Returns (is_correct, reasoning).
        """
        with dspy.context(lm=self.lm):
            result = self._abstention_correctness(
                question=question,
                model_response=response,
            )
        return result.is_correct_abstention, result.reasoning

    def classify_abstention(self, response: str) -> tuple[bool, str]:
        """Classify a hypothesis as abstention vs attempted-answer.

        Returns (is_abstention, reasoning). Pure labelling; does not
        consult the ground truth.
        """
        with dspy.context(lm=self.lm):
            result = self._abstention_classifier(model_response=response)
        return result.is_abstention, result.reasoning
