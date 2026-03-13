"""Check functions — deterministic and LLM-judged evaluation of Memex results."""

from __future__ import annotations

import logging
import time

from memex_common.schemas import EntityDTO, MemoryUnitDTO, NoteSearchResult

from memex_eval.judge import Judge
from memex_eval.internal.scenarios import GroundTruthCheck
from memex_eval.metrics import CheckResult, CheckStatus

logger = logging.getLogger('memex_eval.checks')


from typing import Callable


def run_check(
    check: GroundTruthCheck,
    group_name: str,
    memory_results: list[MemoryUnitDTO] | None = None,
    note_results: list[NoteSearchResult] | None = None,
    entity_names: list[str] | None = None,
    judge: Judge | None = None,
    entities: list[EntityDTO] | None = None,
    cooccurrences: list[dict] | None = None,
    mentions: list[dict] | None = None,
) -> CheckResult:
    """Execute a single ground-truth check and return the result."""
    start = time.monotonic()
    try:
        handler = _CHECK_DISPATCH.get(check.check_type)
        if handler is None:
            result = CheckResult(
                name=check.name,
                group=group_name,
                status=CheckStatus.ERROR,
                description=check.description,
                query=check.query,
                expected=check.expected,
                actual=f'Unknown check type: {check.check_type}',
            )
        else:
            result = handler(
                check=check,
                group_name=group_name,
                memory_results=memory_results,
                note_results=note_results,
                entity_names=entity_names or [],
                judge=judge,
                entities=entities,
                cooccurrences=cooccurrences,
                mentions=mentions,
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
    **_kwargs,
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


def _check_keyword_absent(
    check: GroundTruthCheck,
    group_name: str,
    memory_results: list[MemoryUnitDTO] | None,
    note_results: list[NoteSearchResult] | None,
    **_kwargs,
) -> CheckResult:
    """Check that none of the expected keywords appear in results (isolation test)."""
    combined = _results_text(memory_results, note_results)
    combined_lower = combined.lower()

    expected_list = check.expected if isinstance(check.expected, list) else [check.expected]
    leaked = []
    absent = []
    for keyword in expected_list:
        if keyword.lower() in combined_lower:
            leaked.append(keyword)
        else:
            absent.append(keyword)

    if not leaked:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.PASS,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Correctly absent: {", ".join(absent)}',
        )
    else:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Leaked keywords found: {", ".join(leaked)}',
        )


def _check_entity_exists(
    check: GroundTruthCheck,
    group_name: str,
    entity_names: list[str],
    **_kwargs,
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
    **_kwargs,
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
    **_kwargs,
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


def _check_entity_type(
    check: GroundTruthCheck,
    group_name: str,
    entities: list[EntityDTO] | None,
    **_kwargs,
) -> CheckResult:
    """Check that an entity has the expected type."""
    if not entities:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual='No entities returned',
        )

    expected_name = check.expected if isinstance(check.expected, str) else check.expected[0]
    matched = None
    for entity in entities:
        if expected_name.lower() in entity.name.lower():
            matched = entity
            break

    if matched is None:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Entity "{expected_name}" not found. '
            f'Available: {", ".join(e.name for e in entities[:10])}',
        )

    actual_type = matched.entity_type or ''
    expected_type = check.expected_entity_type or ''
    if actual_type.lower() == expected_type.lower():
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.PASS,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Entity "{matched.name}" has type "{actual_type}"',
        )
    else:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Entity "{matched.name}" has type "{actual_type}", expected "{expected_type}"',
        )


def _check_entity_cooccurrence(
    check: GroundTruthCheck,
    group_name: str,
    cooccurrences: list[dict] | None,
    **_kwargs,
) -> CheckResult:
    """Check that expected entities appear in co-occurrence results."""
    if not cooccurrences:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual='No co-occurrences returned',
        )

    # Collect all co-occurring entity names
    all_names: list[str] = []
    for cooc in cooccurrences:
        for key in ('entity_1_name', 'entity_2_name'):
            name = cooc.get(key, '')
            if name:
                all_names.append(name)

    names_lower = [n.lower() for n in all_names]
    expected_list = check.expected if isinstance(check.expected, list) else [check.expected]

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
            actual=f'Found co-occurrences: {", ".join(found)}',
        )
    else:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Missing co-occurrences: {", ".join(missing)}. '
            f'Available: {", ".join(set(all_names))}',
        )


def _check_entity_mention(
    check: GroundTruthCheck,
    group_name: str,
    mentions: list[dict] | None,
    **_kwargs,
) -> CheckResult:
    """Check that entity mentions contain expected keywords."""
    if not mentions:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual='No mentions returned',
        )

    # Combine all mention texts
    parts: list[str] = []
    for mention in mentions:
        unit = mention.get('unit')
        if unit and hasattr(unit, 'text'):
            parts.append(unit.text)
        elif isinstance(unit, dict) and 'text' in unit:
            parts.append(unit['text'])
    combined = '\n'.join(parts).lower()

    expected_list = check.expected if isinstance(check.expected, list) else [check.expected]

    found = []
    missing = []
    for keyword in expected_list:
        if keyword.lower() in combined:
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
            actual=f'Found keywords in mentions: {", ".join(found)}',
        )
    else:
        return CheckResult(
            name=check.name,
            group=group_name,
            status=CheckStatus.FAIL,
            description=check.description,
            query=check.query,
            expected=check.expected,
            actual=f'Missing keywords: {", ".join(missing)}',
        )


# ---------------------------------------------------------------------------
# Dispatch table: check_type -> handler function
# ---------------------------------------------------------------------------

_CHECK_DISPATCH: dict[str, Callable[..., CheckResult]] = {
    'keyword_in_results': _check_keyword_in_results,
    'keyword_absent_from_results': _check_keyword_absent,
    'entity_exists': _check_entity_exists,
    'entity_type_check': _check_entity_type,
    'entity_cooccurrence_check': _check_entity_cooccurrence,
    'entity_mention_check': _check_entity_mention,
    'result_ordering': _check_result_ordering,
    'llm_judge': _check_llm_judge,
}
