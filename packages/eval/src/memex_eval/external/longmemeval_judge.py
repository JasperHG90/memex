"""LongMemEval Phase 3: Judge hypotheses with an LLM-as-judge.

Reads hypotheses + questions, runs a binary-correctness judge per row, and
writes ``LongMemEvalJudgment`` records to a JSONL file. Reuses
``memex_eval.judge.Judge`` as the underlying executor.

Supports a JSON cache fixture so smoke-tier CI runs do not require live
judge API calls. The cache shape is ``{question_id: {"correct": bool,
"reasoning": str, "judge_model_fingerprint": str}}``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rich.console import Console


from memex_eval.external.longmemeval_common import (
    LongMemEvalJudgment,
    LongMemEvalQuestion,
    _load_variant,
    append_jsonl,
    read_jsonl,
)
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


async def judge_hypotheses(
    dataset_path: str,
    variant: str,
    hypotheses_path: str,
    output_path: str,
    *,
    judge_model: str | None = None,
    cache_path: str | Path | None = None,
    allow_unpinned_checksum: bool = False,
) -> int:
    """Judge hypotheses and write ``LongMemEvalJudgment`` JSONL.

    Returns the number of judgments written. Skips hypotheses that already
    have a judgment in ``output_path``.
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
        )
        append_jsonl(output_path, judgment.model_dump())
        written += 1
        logger.info(
            '[%d/%d] %s (%s, abstention=%s, hyp_abstained=%s) -> correct=%s',
            i + 1,
            len(pending),
            qid,
            question.category.value,
            question.is_abstention,
            is_abstention_hypothesis,
            correct,
        )

    console.print(f'\n[bold green]Wrote {written} judgments -> {output_path}[/bold green]')
    # Surface the last judge fingerprint we used for the report layer.
    logger.debug('Final judge fingerprint: %s', judge_fingerprint)
    return written
