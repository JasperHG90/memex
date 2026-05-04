"""Tests for cognitive-memory check functions."""

from __future__ import annotations

from uuid import uuid4

import pytest

from memex_common.schemas import MemoryUnitDTO

from memex_eval.internal.checks import run_check
from memex_eval.internal.scenarios import GroundTruthCheck
from memex_eval.metrics import CheckStatus


def _make_unit(
    text: str,
    fact_type: str = 'world',
    metadata: dict[str, str] | None = None,
) -> MemoryUnitDTO:
    return MemoryUnitDTO(
        id=uuid4(),
        text=text,
        fact_type=fact_type,
        status='active',
        metadata=metadata or {},
    )


def _check(**overrides) -> GroundTruthCheck:
    defaults = {
        'name': 'test_check',
        'description': 'Test check',
        'query': 'test query',
        'check_type': 'keyword_in_results',
        'expected': 'test keyword',
    }
    defaults.update(overrides)
    return GroundTruthCheck(**defaults)


# ---------------------------------------------------------------------------
# unit_metadata_matches
# ---------------------------------------------------------------------------


class TestUnitMetadataMatches:
    def test_passes_when_metadata_matches(self) -> None:
        unit = _make_unit('Alpha achievement', metadata={'intent_class': 'permanent'})
        check = _check(
            check_type='unit_metadata_matches',
            expected='permanent',
            expected_metadata={'intent_class': 'permanent'},
        )
        result = run_check(check, 'test', memory_results=[unit])
        assert result.status == CheckStatus.PASS

    def test_fails_when_no_metadata_match(self) -> None:
        unit = _make_unit('Alpha achievement', metadata={'intent_class': 'ephemeral'})
        check = _check(
            check_type='unit_metadata_matches',
            expected='permanent',
            expected_metadata={'intent_class': 'permanent'},
        )
        result = run_check(check, 'test', memory_results=[unit])
        assert result.status == CheckStatus.FAIL

    def test_fails_when_no_results(self) -> None:
        check = _check(
            check_type='unit_metadata_matches',
            expected='permanent',
            expected_metadata={'intent_class': 'permanent'},
        )
        result = run_check(check, 'test', memory_results=[])
        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# excluded_by_default
# ---------------------------------------------------------------------------


class TestExcludedByDefault:
    def test_passes_when_keywords_absent(self) -> None:
        units = [
            _make_unit('Widget Pro offers real-time analytics'),
            _make_unit('Widget Pro supports horizontal scaling'),
        ]
        check = _check(
            check_type='excluded_by_default',
            expected=['discontinued', 'migration'],
        )
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_fails_when_keywords_leaked(self) -> None:
        units = [
            _make_unit('Widget Pro offers real-time analytics'),
            _make_unit('Widget Lite discontinued migration path available'),
        ]
        check = _check(
            check_type='excluded_by_default',
            expected=['discontinued', 'migration'],
        )
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.FAIL

    def test_passes_with_empty_results(self) -> None:
        check = _check(
            check_type='excluded_by_default',
            expected=['discontinued'],
        )
        result = run_check(check, 'test', memory_results=[])
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# ranking_after_outcomes
# ---------------------------------------------------------------------------


class TestRankingAfterOutcomes:
    def test_passes_when_high_ranks_before_low(self) -> None:
        units = [
            _make_unit('Project Zeta achievement uptime exceeded targets'),
            _make_unit('Project Zeta incident outage connection pool failure'),
        ]
        check = _check(
            check_type='ranking_after_outcomes',
            expected=['achievement', 'incident'],
        )
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_fails_when_low_ranks_before_high(self) -> None:
        units = [
            _make_unit('Project Zeta incident outage failure'),
            _make_unit('Project Zeta achievement uptime success'),
        ]
        check = _check(
            check_type='ranking_after_outcomes',
            expected=['achievement', 'incident'],
        )
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.FAIL

    def test_passes_when_low_kw_absent(self) -> None:
        units = [
            _make_unit('Project Zeta achievement uptime success'),
        ]
        check = _check(
            check_type='ranking_after_outcomes',
            expected=['achievement', 'incident'],
        )
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_fails_when_high_kw_absent(self) -> None:
        units = [
            _make_unit('Project Zeta incident outage'),
        ]
        check = _check(
            check_type='ranking_after_outcomes',
            expected=['achievement', 'incident'],
        )
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.FAIL

    def test_error_when_expected_not_list_of_two(self) -> None:
        check = _check(
            check_type='ranking_after_outcomes',
            expected='single_keyword',
        )
        result = run_check(check, 'test', memory_results=[])
        assert result.status == CheckStatus.ERROR


# ---------------------------------------------------------------------------
# summary_nonempty
# ---------------------------------------------------------------------------


class TestSummaryNonempty:
    def test_passes_with_nonempty_summary(self) -> None:
        check = _check(
            check_type='summary_nonempty',
            expected='non-empty summary',
        )
        result = run_check(
            check,
            'test',
            summary_result={'summary': 'DataForge is a distributed platform.'},
        )
        assert result.status == CheckStatus.PASS

    def test_fails_with_empty_summary(self) -> None:
        check = _check(
            check_type='summary_nonempty',
            expected='non-empty summary',
        )
        result = run_check(
            check,
            'test',
            summary_result={'summary': ''},
        )
        assert result.status == CheckStatus.FAIL

    def test_fails_with_no_summary(self) -> None:
        check = _check(
            check_type='summary_nonempty',
            expected='non-empty summary',
        )
        result = run_check(check, 'test', summary_result=None)
        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# kv_roundtrip
# ---------------------------------------------------------------------------


class TestKvRoundtrip:
    def test_passes_when_value_matches(self) -> None:
        check = _check(
            check_type='kv_roundtrip',
            expected='deploy with --no-migrate',
        )
        result = run_check(
            check,
            'test',
            kv_result={'value': 'deploy with --no-migrate'},
        )
        assert result.status == CheckStatus.PASS

    def test_fails_when_value_mismatched(self) -> None:
        check = _check(
            check_type='kv_roundtrip',
            expected='deploy with --no-migrate',
        )
        result = run_check(
            check,
            'test',
            kv_result={'value': 'something else'},
        )
        assert result.status == CheckStatus.FAIL

    def test_fails_when_no_result(self) -> None:
        check = _check(
            check_type='kv_roundtrip',
            expected='expected value',
        )
        result = run_check(check, 'test', kv_result=None)
        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# lint_finding_present
# ---------------------------------------------------------------------------


class TestLintFindingPresent:
    def test_passes_when_matching_rule_found(self) -> None:
        findings = [
            {'rule_name': 'contradiction', 'severity': 'high'},
            {'rule_name': 'orphan_entity', 'severity': 'medium'},
        ]
        check = _check(
            check_type='lint_finding_present',
            expected='lint finding',
            expected_metadata={'rule_name': 'contradiction'},
        )
        result = run_check(check, 'test', lint_results={'findings': findings})
        assert result.status == CheckStatus.PASS

    def test_passes_when_severity_matches(self) -> None:
        findings = [
            {'rule_name': 'contradiction', 'severity': 'high'},
        ]
        check = _check(
            check_type='lint_finding_present',
            expected='lint finding',
            expected_metadata={'rule_name': 'contradiction', 'severity': 'high'},
        )
        result = run_check(check, 'test', lint_results={'findings': findings})
        assert result.status == CheckStatus.PASS

    def test_fails_when_no_matching_rule(self) -> None:
        findings = [
            {'rule_name': 'orphan_entity', 'severity': 'medium'},
        ]
        check = _check(
            check_type='lint_finding_present',
            expected='lint finding',
            expected_metadata={'rule_name': 'contradiction'},
        )
        result = run_check(check, 'test', lint_results={'findings': findings})
        assert result.status == CheckStatus.FAIL

    def test_fails_when_no_findings(self) -> None:
        check = _check(
            check_type='lint_finding_present',
            expected='lint finding',
        )
        result = run_check(check, 'test', lint_results={'findings': []})
        assert result.status == CheckStatus.FAIL

    def test_fails_with_none_lint_results(self) -> None:
        check = _check(
            check_type='lint_finding_present',
            expected='lint finding',
        )
        result = run_check(check, 'test', lint_results=None)
        assert result.status == CheckStatus.FAIL

    def test_passes_with_any_finding_when_no_metadata_filter(self) -> None:
        findings = [
            {'rule_name': 'orphan_entity', 'severity': 'medium'},
        ]
        check = _check(
            check_type='lint_finding_present',
            expected='lint finding',
        )
        result = run_check(check, 'test', lint_results={'findings': findings})
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# llm_lint_flags_unit
# ---------------------------------------------------------------------------


class TestLlmLintFlagsUnit:
    def test_skip_without_judge(self) -> None:
        check = _check(
            check_type='llm_lint_flags_unit',
            expected='surprise finding',
            expected_metadata={'rule_name': 'surprise_gate_llm'},
        )
        result = run_check(check, 'test', judge=None, lint_results={'findings': []})
        assert result.status == CheckStatus.SKIP

    def test_passes_when_surprise_rule_found(self) -> None:
        findings = [
            {'rule_name': 'surprise_gate_llm', 'severity': 'medium'},
        ]
        check = _check(
            check_type='llm_lint_flags_unit',
            expected='surprise finding',
            expected_metadata={'rule_name': 'surprise_gate_llm'},
        )
        # Use a real Judge instance is not needed — the check only looks at lint_results
        from memex_eval.judge import Judge

        try:
            judge = Judge.__new__(Judge)
        except Exception:
            pytest.skip('Judge requires API key')
        result = run_check(
            check,
            'test',
            judge=judge,
            lint_results={'findings': findings},
        )
        assert result.status == CheckStatus.PASS

    def test_fails_when_no_matching_rule(self) -> None:
        findings = [
            {'rule_name': 'orphan_entity', 'severity': 'medium'},
        ]
        check = _check(
            check_type='llm_lint_flags_unit',
            expected='surprise finding',
        )
        result = run_check(check, 'test', judge=object(), lint_results={'findings': findings})
        assert result.status == CheckStatus.FAIL

    def test_fails_when_no_findings(self) -> None:
        check = _check(
            check_type='llm_lint_flags_unit',
            expected='surprise finding',
        )
        result = run_check(check, 'test', judge=object(), lint_results={'findings': []})
        assert result.status == CheckStatus.FAIL
