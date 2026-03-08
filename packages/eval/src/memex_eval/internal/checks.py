"""Check functions — deterministic and LLM-judged evaluation of Memex results."""

from __future__ import annotations

import logging
import time

from memex_common.schemas import MemoryUnitDTO, NoteSearchResult

from memex_eval.judge import Judge
from memex_eval.internal.scenarios import GroundTruthCheck
from memex_eval.metrics import CheckResult, CheckStatus

logger = logging.getLogger('memex_eval.checks')


def run_check(
    check: GroundTruthCheck,
    group_name: str,
    memory_results: list[MemoryUnitDTO] | None = None,
    note_results: list[NoteSearchResult] | None = None,
    entity_names: list[str] | None = None,
    judge: Judge | None = None,
) -> CheckResult:
    """Execute a single ground-truth check and return the result."""
    start = time.monotonic()
    try:
        if check.check_type == 'keyword_in_results':
            result = _check_keyword_in_results(check, group_name, memory_results, note_results)
        elif check.check_type == 'entity_exists':
            result = _check_entity_exists(check, group_name, entity_names or [])
        elif check.check_type == 'result_ordering':
            result = _check_result_ordering(check, group_name, memory_results)
        elif check.check_type == 'llm_judge':
            result = _check_llm_judge(check, group_name, memory_results, note_results, judge)
        else:
            result = CheckResult(
                name=check.name,
                group=group_name,
                status=CheckStatus.ERROR,
                description=check.description,
                query=check.query,
                expected=check.expected,
                actual=f'Unknown check type: {check.check_type}',
            )
    except Exception as e:
        result = CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.ERROR,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=str(e),
        )

    result.duration_ms = (time.monotonic() - start) * 1000
    return result


def _results_text(
    memory_results: list[MemoryUnitDTO] | None,
    note_results: list[NoteSearchResult] | None,
) -> str:
    """Combine all result texts into a single searchable string."""
    parts: list[str] = []
    if memory_results:
        parts.extend(r.text for r in memory_results)
    if note_results:
        for nr in note_results:
            parts.extend(s.text for s in nr.snippets)
            if nr.metadata:
                parts.append(str(nr.metadata))
    return '\n'.join(parts)


def _check_keyword_in_results(
    check: GroundTruthCheck,
    group_name: str,
    memory_results: list[MemoryUnitDTO] | None,
    note_results: list[NoteSearchResult] | None,
) -> CheckResult:
    """Check that all expected keywords appear in top-K results."""
    combined = _results_text(memory_results, note_results)
    combined_lower = combined.lower()

    expected_list = check.expected if isinstance(check.expected, list) else [check.expected]
    found = []
    missing = []
    for keyword in expected_list:
        if keyword.lower() in combined_lower:
            found.append(keyword)
        else:
            missing.append(keyword)

    if not missing:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.PASS,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Found all: {", ".join(found)}',
        )
    else:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Missing: {", ".join(missing)}',
        )


def _check_entity_exists(
    check: GroundTruthCheck,
    group_name: str,
    entity_names: list[str],
) -> CheckResult:
    """Check that expected entities exist in the system."""
    expected_list = check.expected if isinstance(check.expected, list) else [check.expected]
    names_lower = [n.lower() for n in entity_names]

    found = []
    missing = []
    for expected in expected_list:
        if any(expected.lower() in name for name in names_lower):
            found.append(expected)
        else:
            missing.append(expected)

    if not missing:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.PASS,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Found entities: {", ".join(found)}',
        )
    else:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Missing entities: {", ".join(missing)}. '
            f'Available: {", ".join(entity_names[:20])}',
        )


def _check_result_ordering(
    check: GroundTruthCheck,
    group_name: str,
    memory_results: list[MemoryUnitDTO] | None,
) -> CheckResult:
    """Check that results appear in the expected order (first expected before second)."""
    if not isinstance(check.expected, list) or len(check.expected) < 2:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.ERROR,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual='result_ordering requires a list of >= 2 expected items',
        )

    if not memory_results:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual='No results returned',
        )

    # Find first occurrence index of each expected keyword
    texts = [r.text.lower() for r in memory_results]
    first_keyword = check.expected[0].lower()
    second_keyword = check.expected[1].lower()

    first_idx = None
    second_idx = None
    for i, text in enumerate(texts):
        if first_idx is None and first_keyword in text:
            first_idx = i
        if second_idx is None and second_keyword in text:
            second_idx = i

    if first_idx is None:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'"{check.expected[0]}" not found in results',
        )
    if second_idx is None:
        # First found but second not — first is ranked, which is a partial pass
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.PASS,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'"{check.expected[0]}" at rank {first_idx + 1}, '
            f'"{check.expected[1]}" not in results (correctly downranked)',
        )

    if first_idx <= second_idx:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.PASS,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Correct order: "{check.expected[0]}" at rank {first_idx + 1}, '
            f'"{check.expected[1]}" at rank {second_idx + 1}',
        )
    else:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Wrong order: "{check.expected[1]}" at rank {second_idx + 1} '
            f'appears before "{check.expected[0]}" at rank {first_idx + 1}',
        )


def _check_llm_judge(
    check: GroundTruthCheck,
    group_name: str,
    memory_results: list[MemoryUnitDTO] | None,
    note_results: list[NoteSearchResult] | None,
    judge: Judge | None,
) -> CheckResult:
    """Use LLM judge to evaluate result quality."""
    if judge is None:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.SKIP,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual='Skipped (no LLM judge configured)',
        )

    combined = _results_text(memory_results, note_results)
    if not combined.strip():
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual='No results to judge',
        )

    expected_str = check.expected if isinstance(check.expected, str) else ', '.join(check.expected)
    is_correct, reasoning = judge.judge_relevance(
        query=check.query,
        expected=expected_str,
        search_result=combined[:3000],
    )

    return CheckResult(
        name=check.name,
        group=group_name,
        status=CheckStatus.PASS if is_correct else CheckStatus.FAIL,
        description=check.description,
        query=check.query,
        expected=check.expected,
        actual=combined[:200],
        reasoning=reasoning,
    )
