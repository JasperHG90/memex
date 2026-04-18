"""LongMemEval Phase 3: Judge hypotheses with an LLM-as-judge.

Reads hypotheses + questions, runs a binary-correctness judge per row, and
writes ``LongMemEvalJudgment`` records to a JSONL file. Reuses
``memex_eval.judge.Judge`` as the underlying executor.

Additionally performs retrieval containment judging: determines whether
the memex retrieval output contains sufficient evidence to answer the
question. Combined with answer correctness, this yields a 2x2 error
analysis (correct, model_error, correct_abstention, hallucination,
lucky_guess).

Supports a JSON cache fixture so smoke-tier CI runs do not require live
judge API calls. The cache shape is ``{question_id: {"correct": bool,
"reasoning": str, "judge_model_fingerprint": str}}``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import dspy
from rich.console import Console


from memex_eval.external.longmemeval_common import (
    LongMemEvalJudgment,
    LongMemEvalQuestion,
    _load_variant,
    append_jsonl,
    read_jsonl,
)
from memex_eval.external.longmemeval_trace_parser import extract_retrieval_content
from memex_eval.judge import Judge

logger = logging.getLogger('memex_eval.longmemeval_judge')
console = Console()


JUDGE_PROMPT_TEMPLATE_VERSION = '2026-04-15.v1'

# The template the underlying dspy.Signature wraps. Documented here so that
# any prompt drift versus the upstream LongMemEval evaluate_qa.py reference
# is visible at code-review time.
JUDGE_PROMPT_GUIDANCE = """\
You are evaluating whether a hypothesis answer correctly addresses a
LongMemEval question given the ground-truth answer.

Mark the hypothesis as CORRECT when:
- It conveys the same information as the ground truth (paraphrasing OK).
- For abstention questions where the ground truth is missing/null and the
  hypothesis explicitly admits "I do not know" (or close paraphrase).

Mark the hypothesis as INCORRECT when:
- It contradicts the ground truth.
- It hallucinates specifics not supported by the ground truth.
- It refuses to answer when the ground truth provides a clear answer.
"""


def _load_cache(cache_path: str | Path | None) -> dict[str, dict[str, Any]]:
    if cache_path is None:
        return {}
    p = Path(cache_path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _heuristic_abstention_fallback(hypothesis: str) -> bool:
    """Heuristic abstention classifier used ONLY as a fallback when the LM
    judge errors out. The primary path routes all hypotheses through the
    LM (see ``Judge.classify_abstention``); this exists so a transient LM
    failure does not zero out the abstention metric.
    """
    h = hypothesis.lower()
    return any(
        marker in h
        for marker in (
            'i do not know',
            "i don't know",
            'cannot answer',
            'not enough information',
            'no information',
        )
    )


# ---------------------------------------------------------------------------
# Retrieval containment judge
# ---------------------------------------------------------------------------


class RetrievalContainment(dspy.Signature):
    """Judge whether retrieval results contain sufficient evidence to answer a question."""

    question: str = dspy.InputField(desc='The question that was asked.')
    expected_answer: str = dspy.InputField(desc='The ground truth expected answer.')
    retrieval_results: str = dspy.InputField(
        desc='All memex tool result text from the session trace.'
    )
    contains_answer: bool = dspy.OutputField(
        desc='Whether the retrieval output contains the information needed to answer correctly.'
    )
    reasoning: str = dspy.OutputField(
        desc='Brief explanation of what evidence is present or missing.'
    )


async def judge_retrieval_containment(
    question_text: str,
    gold_answer: str,
    retrieval_content: str,
    judge_model: str | None = None,
    *,
    judge_instance: Judge | None = None,
) -> tuple[bool, str]:
    """Judge whether retrieval results contain the answer.

    Returns (contains, reasoning).
    """
    if not retrieval_content.strip():
        return False, 'No retrieval content found in trace.'

    if judge_instance is None:
        judge_instance = Judge(model=judge_model)

    predictor = dspy.ChainOfThought(RetrievalContainment)
    with dspy.context(lm=judge_instance.lm):
        result = predictor(
            question=question_text,
            expected_answer=gold_answer or 'The question has no known answer (abstention).',
            retrieval_results=retrieval_content,
        )
    return result.contains_answer, result.reasoning


def compute_interpretation(
    retrieval_contains: bool,
    answer_correct: bool,
    is_abstention_hypothesis: bool,
) -> str:
    """Compute the interpretation label from the 2x2 matrix.

    Returns one of: correct, model_error, correct_abstention,
    hallucination, lucky_guess.
    """
    if retrieval_contains and answer_correct:
        return 'correct'
    if retrieval_contains and not answer_correct:
        return 'model_error'
    if not retrieval_contains and is_abstention_hypothesis:
        return 'correct_abstention'
    if not retrieval_contains and answer_correct:
        return 'lucky_guess'
    # not retrieval_contains and not answer_correct and not abstention
    return 'hallucination'


async def judge_hypotheses(
    dataset_path: str,
    variant: str,
    hypotheses_path: str,
    output_path: str,
    *,
    judge_model: str | None = None,
    cache_path: str | Path | None = None,
    allow_unpinned_checksum: bool = False,
    traces_dir: str | Path | None = None,
) -> int:
    """Judge hypotheses and write ``LongMemEvalJudgment`` JSONL.

    Returns the number of judgments written. Skips hypotheses that already
    have a judgment in ``output_path``.

    When ``traces_dir`` is provided, also performs retrieval containment
    judging: extracts all memex tool results from the session trace and
    asks an LLM whether they contain the evidence needed to answer.
    The result populates ``retrieval_contains_answer``,
    ``retrieval_containment_reasoning``, and ``interpretation`` fields.
    """
    questions: dict[str, LongMemEvalQuestion] = {
        q.question_id: q
        for q in _load_variant(Path(dataset_path), variant, allow_unpinned=allow_unpinned_checksum)
    }
    hypotheses = read_jsonl(hypotheses_path)
    if not hypotheses:
        raise ValueError(f'No hypotheses found in {hypotheses_path}')

    already_judged = {r['question_id'] for r in read_jsonl(output_path) if 'question_id' in r}
    pending = [h for h in hypotheses if h['question_id'] not in already_judged]
    if not pending:
        console.print('[dim]All hypotheses already judged.[/dim]')
        return 0

    cache = _load_cache(cache_path)

    judge: Judge | None = None

    # The LM is needed for any pending row not fully cached. A cache hit must
    # also provide ``is_abstention_hypothesis`` to skip the classifier call.
    def _cache_entry_is_complete(entry: dict[str, Any]) -> bool:
        return 'correct' in entry and 'is_abstention_hypothesis' in entry

    needs_lm = any(
        h['question_id'] not in cache or not _cache_entry_is_complete(cache[h['question_id']])
        for h in pending
    )
    if needs_lm:
        judge = Judge(model=judge_model)

    judge_fingerprint = judge_model or 'cached'
    written = 0

    for i, h in enumerate(pending):
        qid = h['question_id']
        question = questions.get(qid)
        if question is None:
            logger.warning('Hypothesis %s has no matching question; skipping.', qid)
            continue

        cache_entry = cache.get(qid)
        if cache_entry is not None and _cache_entry_is_complete(cache_entry):
            correct = bool(cache_entry['correct'])
            reasoning = cache_entry.get('reasoning', '')
            fingerprint = cache_entry.get('judge_model_fingerprint', 'cached')
            is_abstention_hypothesis = bool(cache_entry['is_abstention_hypothesis'])
        else:
            assert judge is not None
            # Every hypothesis (abstention question or not) is run through the
            # classifier so abstention precision has an independent denominator.
            try:
                is_abstention_hypothesis, _ = judge.classify_abstention(h['hypothesis'])
            except Exception as exc:
                logger.warning(
                    'Abstention classifier failed for %s: %s — using heuristic fallback.',
                    qid,
                    exc,
                )
                is_abstention_hypothesis = _heuristic_abstention_fallback(h['hypothesis'])

            if question.is_abstention and not question.answer:
                # No ground-truth string — ask the LM whether the hypothesis
                # correctly abstains for this specific question.
                try:
                    correct, reasoning = judge.judge_abstention_correctness(
                        question=question.question_text,
                        response=h['hypothesis'],
                    )
                except Exception as exc:
                    logger.warning(
                        'Abstention judge failed for %s: %s — using heuristic fallback.',
                        qid,
                        exc,
                    )
                    correct = _heuristic_abstention_fallback(h['hypothesis'])
                    reasoning = (
                        'Abstention judge LM failed; heuristic fallback '
                        f'returned correct={correct}.'
                    )
            else:
                correct, reasoning = judge.judge_correctness(
                    question=question.question_text,
                    expected=question.answer or '',
                    response=h['hypothesis'],
                )
            fingerprint = (
                f'{getattr(judge.lm, "model", judge_model or "unknown")}'
                f'@{JUDGE_PROMPT_TEMPLATE_VERSION}'
            )
            judge_fingerprint = fingerprint

        # --- Retrieval containment judging ---
        retrieval_contains = True
        containment_reasoning = ''
        if traces_dir is not None:
            traces_path = Path(traces_dir)
            trace_file = traces_path / f'{qid}.jsonl'
            if trace_file.exists():
                retrieval_content = extract_retrieval_content(trace_file)

                cache_containment = (cache.get(qid) or {}).get('retrieval_contains_answer')
                if cache_containment is not None:
                    retrieval_contains = bool(cache_containment)
                    containment_reasoning = (cache.get(qid) or {}).get(
                        'retrieval_containment_reasoning', ''
                    )
                elif judge is not None:
                    try:
                        (
                            retrieval_contains,
                            containment_reasoning,
                        ) = await judge_retrieval_containment(
                            question_text=question.question_text,
                            gold_answer=question.answer or '',
                            retrieval_content=retrieval_content,
                            judge_instance=judge,
                        )
                    except Exception as exc:
                        logger.warning(
                            'Retrieval containment judge failed for %s: %s',
                            qid,
                            exc,
                        )
                        retrieval_contains = True
                        containment_reasoning = f'Retrieval containment judge failed: {exc}'
            else:
                logger.debug('No trace file for %s; skipping containment judge.', qid)

        interpretation = compute_interpretation(
            retrieval_contains=retrieval_contains,
            answer_correct=correct,
            is_abstention_hypothesis=is_abstention_hypothesis,
        )

        judgment = LongMemEvalJudgment(
            question_id=qid,
            category=question.category,
            is_abstention=question.is_abstention,
            hypothesis=h['hypothesis'],
            expected=question.answer,
            correct=correct,
            judge_reasoning=reasoning,
            judge_model_fingerprint=fingerprint,
            is_abstention_hypothesis=is_abstention_hypothesis,
            retrieval_contains_answer=retrieval_contains,
            retrieval_containment_reasoning=containment_reasoning,
            interpretation=interpretation,
        )
        append_jsonl(output_path, judgment.model_dump())
        written += 1
        logger.info(
            '[%d/%d] %s (%s, abstention=%s, hyp_abstained=%s) -> '
            'correct=%s retrieval_contains=%s interpretation=%s',
            i + 1,
            len(pending),
            qid,
            question.category.value,
            question.is_abstention,
            is_abstention_hypothesis,
            correct,
            retrieval_contains,
            interpretation,
        )

    console.print(f'\n[bold green]Wrote {written} judgments -> {output_path}[/bold green]')
    # Surface the last judge fingerprint we used for the report layer.
    logger.debug('Final judge fingerprint: %s', judge_fingerprint)
    return written
