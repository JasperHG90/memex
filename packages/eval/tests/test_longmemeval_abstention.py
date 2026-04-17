"""Tests for the LongMemEval abstention metric decoupling (C3).

Before C3 the report derived abstention precision via the same
heuristic that gated correctness, so the numerator and denominator
were tautologically coupled and could not actually detect
hallucination-on-abstention-question. These tests exercise two
hypothesis shapes against the same ground-truth set and verify that
precision and recall diverge — i.e. that the metric discriminates.
"""

from __future__ import annotations

from memex_eval.external.longmemeval_common import (
    LongMemEvalCategory,
    LongMemEvalJudgment,
)
from memex_eval.external.longmemeval_report import aggregate_for_test


def _mk_judgment(
    qid: str,
    *,
    is_abstention: bool,
    hypothesis: str,
    correct: bool,
    is_abstention_hypothesis: bool,
) -> LongMemEvalJudgment:
    return LongMemEvalJudgment(
        question_id=qid,
        category=LongMemEvalCategory.KNOWLEDGE_UPDATE,
        is_abstention=is_abstention,
        hypothesis=hypothesis,
        expected=None if is_abstention else 'ground-truth',
        correct=correct,
        judge_reasoning='fixture',
        judge_model_fingerprint='test',
        is_abstention_hypothesis=is_abstention_hypothesis,
    )


def test_abstention_metrics_distinguish_correct_vs_hallucinated() -> None:
    """Two fixtures — one correctly abstains, one hallucinates — must
    produce different precision/recall even though the ground-truth set
    is identical. This test would fail against the pre-C3
    implementation where precision was computed via the same heuristic
    used for correctness.
    """
    # Correct-abstention fixture.
    correct_fixture = [
        _mk_judgment(
            'q-abs-001_abs',
            is_abstention=True,
            hypothesis='I do not know based on the available memory.',
            correct=True,
            is_abstention_hypothesis=True,
        ),
        _mk_judgment(
            'q-nonabs-001',
            is_abstention=False,
            hypothesis='Tuesday.',
            correct=True,
            is_abstention_hypothesis=False,
        ),
    ]

    # Hallucinating fixture: on the abstention question, the hypothesis
    # confidently makes something up. The judge (correctly) marks it
    # incorrect, and the LM abstention classifier (correctly) marks it
    # as NOT an abstention.
    hallucinating_fixture = [
        _mk_judgment(
            'q-abs-001_abs',
            is_abstention=True,
            hypothesis='The answer is clearly Wednesday at noon.',
            correct=False,
            is_abstention_hypothesis=False,
        ),
        _mk_judgment(
            'q-nonabs-001',
            is_abstention=False,
            hypothesis='Tuesday.',
            correct=True,
            is_abstention_hypothesis=False,
        ),
    ]

    correct_agg = aggregate_for_test(correct_fixture)
    halluc_agg = aggregate_for_test(hallucinating_fixture)

    # Recall must drop: the ground-truth abstention was not answered correctly.
    assert correct_agg['abstention_recall'] == 1.0
    assert halluc_agg['abstention_recall'] == 0.0

    # Precision must also drop — but crucially, the two numbers are
    # different. Under the pre-C3 coupling both would be identical.
    assert correct_agg['abstention_precision'] != halluc_agg['abstention_precision']


def test_precision_denominator_uses_lm_classified_abstentions() -> None:
    """A non-abstention question whose hypothesis is phrased as an
    abstention (e.g. a cautious LM) drags precision down — this is the
    behaviour we want. The denominator is driven by
    ``is_abstention_hypothesis``, not by string-matching correctness.
    """
    judgments = [
        # Ground-truth abstention + hypothesis correctly abstains.
        _mk_judgment(
            'q-abs-001_abs',
            is_abstention=True,
            hypothesis='I do not know.',
            correct=True,
            is_abstention_hypothesis=True,
        ),
        # Ground-truth has an answer, but hypothesis is cautious —
        # counted as an abstention hypothesis (wrongly abstained).
        _mk_judgment(
            'q-nonabs-001',
            is_abstention=False,
            hypothesis='I do not have enough information to say.',
            correct=False,
            is_abstention_hypothesis=True,
        ),
    ]
    agg = aggregate_for_test(judgments)
    # Two abstention hypotheses, one of which maps to a true
    # abstention question -> precision = 0.5.
    assert agg['abstention_precision'] == 0.5
    # Only one ground-truth abstention, correctly answered -> recall = 1.0.
    assert agg['abstention_recall'] == 1.0


def test_judgment_default_is_abstention_hypothesis_is_false() -> None:
    """The new field is additive; existing callers that omit it still
    parse, with the safe default that nothing is counted as an
    abstention hypothesis."""
    j = LongMemEvalJudgment(
        question_id='q',
        category=LongMemEvalCategory.MULTI_SESSION,
        is_abstention=False,
        hypothesis='x',
        expected='x',
        correct=True,
        judge_reasoning='',
        judge_model_fingerprint='t',
    )
    assert j.is_abstention_hypothesis is False
