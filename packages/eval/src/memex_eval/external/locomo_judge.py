"""LoCoMo Phase 3: Judge answers and produce a report.

Reads questions and answers from JSONL, evaluates with graded correctness
and tool pattern analysis, and writes a report.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from memex_eval.external.locomo_common import (
    CATEGORY_NAMES,
    QUESTION_TYPES,
    read_jsonl,
)
from memex_eval.judge import Judge

logger = logging.getLogger('memex_eval.locomo_judge')
console = Console()

VALID_SCORES = [0.0, 0.25, 0.5, 0.75, 1.0]


def _clamp_score(score: float) -> float:
    """Clamp a score to the nearest valid value."""
    return min(VALID_SCORES, key=lambda v: abs(v - score))


def analyze_tool_pattern(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze tool call patterns against the recommended AGENTS.md workflow.

    Checks:
    1. Used memex_memory_search?
    2. Used memex_note_search?
    3. Used two-speed verification (get_page_index -> get_node)?
    4. Was two-speed ordered correctly?
    """
    sequence = [tc.get('name', '') for tc in tool_calls]

    memory_search = 'memex_memory_search' in sequence
    note_search = 'memex_note_search' in sequence

    has_page_index = 'memex_get_page_indices' in sequence
    has_get_node = 'memex_get_nodes' in sequence
    two_speed = has_page_index and has_get_node

    # Check ordering: page_indices before get_nodes
    two_speed_ordered = False
    if two_speed:
        first_page = next(i for i, s in enumerate(sequence) if s == 'memex_get_page_indices')
        first_node = next(i for i, s in enumerate(sequence) if s == 'memex_get_nodes')
        two_speed_ordered = first_page < first_node

    return {
        'memory_search': memory_search,
        'note_search': note_search,
        'two_speed': two_speed,
        'two_speed_ordered': two_speed_ordered,
        'total_calls': len(tool_calls),
        'sequence': sequence,
    }


async def judge_answers(
    questions_path: str,
    answers_path: str,
    output_path: str,
    judge_model: str | None = None,
) -> dict[str, Any]:
    """Judge answers and produce a report.

    Returns the full report dict.
    """
    questions = {q['id']: q for q in read_jsonl(questions_path)}
    answers = {a['id']: a for a in read_jsonl(answers_path)}

    if not answers:
        raise ValueError(f'No answers found in {answers_path}')

    judge = Judge(model=judge_model)

    # Per-category tracking
    category_scores: dict[str, list[float]] = {qt: [] for qt in QUESTION_TYPES}
    category_counts: dict[str, int] = {qt: 0 for qt in QUESTION_TYPES}

    # Tool pattern tracking
    tool_patterns: list[dict[str, Any]] = []

    # Token tracking
    total_input_tokens = 0
    total_output_tokens = 0
    total_duration = 0.0

    # Per-question details
    details: list[dict[str, Any]] = []

    matched_ids = sorted(set(questions.keys()) & set(answers.keys()))
    console.print(f'[bold]Judging {len(matched_ids)} answers...[/bold]')

    for i, qid in enumerate(matched_ids):
        q = questions[qid]
        a = answers[qid]

        category = q.get('category', 'single_hop')
        question_text = q['question']
        expected = q['expected']
        answer_text = a.get('answer', '')
        tool_calls = a.get('tool_calls', [])
        tokens = a.get('tokens', {})
        duration = a.get('duration_s', 0.0)
        error = a.get('error')

        logger.info(
            '[%d/%d] %s (%s): %s',
            i + 1,
            len(matched_ids),
            qid,
            CATEGORY_NAMES.get(category, category),
            question_text[:60],
        )

        # Grade correctness
        if answer_text and not error:
            score, reasoning = judge.judge_graded_correctness(
                question=question_text,
                expected=expected,
                response=answer_text,
            )
            score = _clamp_score(score)
        else:
            score = 0.0
            reasoning = error or 'No answer produced'

        # Tool pattern analysis
        pattern = analyze_tool_pattern(tool_calls)
        tool_patterns.append(pattern)

        # Accumulate stats
        category_scores[category].append(score)
        category_counts[category] += 1
        total_input_tokens += tokens.get('input', 0)
        total_output_tokens += tokens.get('output', 0)
        total_duration += duration

        details.append(
            {
                'id': qid,
                'category': category,
                'question': question_text,
                'expected': expected,
                'answer': answer_text[:500],
                'score': score,
                'reasoning': reasoning,
                'tool_pattern': pattern,
                'tokens': tokens,
                'duration_s': duration,
                'error': error,
            }
        )

        logger.info('  -> score=%.2f %s', score, reasoning[:60])

    # Aggregate scores by category
    category_results: dict[str, dict[str, Any]] = {}
    for qt in QUESTION_TYPES:
        scores = category_scores[qt]
        if scores:
            category_results[qt] = {
                'count': len(scores),
                'mean_score': round(sum(scores) / len(scores), 3),
                'perfect': sum(1 for s in scores if s == 1.0),
                'partial': sum(1 for s in scores if 0.0 < s < 1.0),
                'wrong': sum(1 for s in scores if s == 0.0),
            }

    # Aggregate tool patterns
    n = len(tool_patterns)
    tool_summary = {
        'memory_search_rate': round(sum(1 for p in tool_patterns if p['memory_search']) / n, 3),
        'note_search_rate': round(sum(1 for p in tool_patterns if p['note_search']) / n, 3),
        'two_speed_rate': round(sum(1 for p in tool_patterns if p['two_speed']) / n, 3),
        'two_speed_ordered_rate': round(
            sum(1 for p in tool_patterns if p['two_speed_ordered']) / n, 3
        ),
        'avg_tool_calls': round(sum(p['total_calls'] for p in tool_patterns) / n, 1),
    }

    # Overall
    all_scores = [d['score'] for d in details]
    overall = {
        'count': len(all_scores),
        'mean_score': round(sum(all_scores) / len(all_scores), 3),
        'perfect': sum(1 for s in all_scores if s == 1.0),
        'partial': sum(1 for s in all_scores if 0.0 < s < 1.0),
        'wrong': sum(1 for s in all_scores if s == 0.0),
    }

    report = {
        'benchmark': 'LoCoMo-Pipeline',
        'questions_file': questions_path,
        'answers_file': answers_path,
        'overall': overall,
        'categories': category_results,
        'tool_patterns': tool_summary,
        'tokens': {
            'total_input': total_input_tokens,
            'total_output': total_output_tokens,
            'total': total_input_tokens + total_output_tokens,
        },
        'total_duration_s': round(total_duration, 1),
        'details': details,
    }

    # Write report
    Path(output_path).write_text(json.dumps(report, indent=2, default=str))

    # Print terminal summary
    _print_report(report)

    return report


def _print_report(report: dict[str, Any]) -> None:
    """Print a rich terminal summary of the judge report."""
    console.print()
    console.rule('[bold]LoCoMo Pipeline Report[/bold]')
    console.print()

    overall = report['overall']
    console.print(
        f'[dim]{overall["count"]} questions | '
        f'Tokens: {report["tokens"]["total"]:,} | '
        f'Duration: {report["total_duration_s"]}s[/dim]'
    )
    console.print()

    # Scores by category
    table = Table(show_header=True, header_style='bold cyan')
    table.add_column('Category', style='white')
    table.add_column('Count', justify='right')
    table.add_column('Mean Score', justify='right')
    table.add_column('Perfect', justify='right')
    table.add_column('Partial', justify='right')
    table.add_column('Wrong', justify='right')

    for qt in QUESTION_TYPES:
        if qt not in report['categories']:
            continue
        cat = report['categories'][qt]
        score = cat['mean_score']
        style = 'green' if score >= 0.7 else ('yellow' if score >= 0.4 else 'red')
        table.add_row(
            CATEGORY_NAMES.get(qt, qt),
            str(cat['count']),
            f'[{style}]{score:.3f}[/{style}]',
            str(cat['perfect']),
            str(cat['partial']),
            str(cat['wrong']),
        )

    score = overall['mean_score']
    style = 'green' if score >= 0.7 else ('yellow' if score >= 0.4 else 'red')
    table.add_row(
        '[bold]Overall[/bold]',
        f'[bold]{overall["count"]}[/bold]',
        f'[bold {style}]{score:.3f}[/bold {style}]',
        f'[bold]{overall["perfect"]}[/bold]',
        f'[bold]{overall["partial"]}[/bold]',
        f'[bold]{overall["wrong"]}[/bold]',
        end_section=True,
    )

    console.print(table)
    console.print()

    # Tool patterns
    tp = report['tool_patterns']
    console.print('[bold]Tool Pattern Compliance[/bold]')
    console.print(f'  Memory search:      {tp["memory_search_rate"]:.0%}')
    console.print(f'  Note search:        {tp["note_search_rate"]:.0%}')
    console.print(f'  Two-speed:          {tp["two_speed_rate"]:.0%}')
    console.print(f'  Two-speed ordered:  {tp["two_speed_ordered_rate"]:.0%}')
    console.print(f'  Avg tool calls:     {tp["avg_tool_calls"]}')
    console.print()

    # Token summary
    t = report['tokens']
    console.print('[bold]Token Usage[/bold]')
    console.print(f'  Input:  {t["total_input"]:,}')
    console.print(f'  Output: {t["total_output"]:,}')
    console.print(f'  Total:  {t["total"]:,}')
    console.print()
