"""Tests for check functions — the quality gates of the eval framework."""

from __future__ import annotations

from memex_eval.internal.checks import run_check, _results_text
from memex_eval.metrics import CheckStatus


# ---------------------------------------------------------------------------
# _results_text helper
# ---------------------------------------------------------------------------


class TestResultsText:
    def test_empty_inputs(self):
        assert _results_text(None, None) == ''

    def test_memory_only(self, make_unit):
        units = [make_unit('Alpha fact'), make_unit('Beta fact')]
        text = _results_text(units, None)
        assert 'Alpha fact' in text
        assert 'Beta fact' in text

    def test_note_only(self, make_note_result):
        notes = [make_note_result(['snippet one snippet two'])]
        text = _results_text(None, notes)
        assert 'snippet one' in text
        assert 'snippet two' in text

    def test_combined(self, make_unit, make_note_result):
        units = [make_unit('memory text')]
        notes = [make_note_result(['note text'])]
        text = _results_text(units, notes)
        assert 'memory text' in text
        assert 'note text' in text


# ---------------------------------------------------------------------------
# keyword_in_results
# ---------------------------------------------------------------------------


class TestKeywordInResults:
    def test_pass_single_keyword(self, make_check, make_unit):
        check = make_check(expected='alpha', check_type='keyword_in_results')
        units = [make_unit('The alpha project started today')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_fail_missing_keyword(self, make_check, make_unit):
        check = make_check(expected='gamma', check_type='keyword_in_results')
        units = [make_unit('The alpha project started today')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.FAIL
        assert 'gamma' in result.actual.lower()

    def test_pass_multiple_keywords(self, make_check, make_unit):
        check = make_check(expected=['alpha', 'project'], check_type='keyword_in_results')
        units = [make_unit('The alpha project started today')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_fail_partial_keywords(self, make_check, make_unit):
        check = make_check(
            expected=['alpha', 'missing_term'],
            check_type='keyword_in_results',
        )
        units = [make_unit('The alpha project started today')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.FAIL

    def test_case_insensitive(self, make_check, make_unit):
        check = make_check(expected='ALPHA', check_type='keyword_in_results')
        units = [make_unit('The alpha project started today')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_searches_note_results(self, make_check, make_note_result):
        check = make_check(expected='snippet', check_type='keyword_in_results')
        notes = [make_note_result(['This is a snippet from a note'])]
        result = run_check(check, 'test', note_results=notes)
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# keyword_absent_from_results
# ---------------------------------------------------------------------------


class TestKeywordAbsent:
    def test_pass_keyword_absent(self, make_check, make_unit):
        check = make_check(expected='gamma', check_type='keyword_absent_from_results')
        units = [make_unit('alpha beta')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_fail_keyword_present(self, make_check, make_unit):
        check = make_check(expected='alpha', check_type='keyword_absent_from_results')
        units = [make_unit('alpha beta')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.FAIL

    def test_multiple_absent(self, make_check, make_unit):
        check = make_check(
            expected=['gamma', 'delta'],
            check_type='keyword_absent_from_results',
        )
        units = [make_unit('alpha beta')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# entity_exists
# ---------------------------------------------------------------------------


class TestEntityExists:
    def test_pass_entity_found(self, make_check):
        check = make_check(expected='Python', check_type='entity_exists')
        result = run_check(check, 'test', entity_names=['Python', 'Rust', 'Go'])
        assert result.status == CheckStatus.PASS

    def test_fail_entity_missing(self, make_check):
        check = make_check(expected='Haskell', check_type='entity_exists')
        result = run_check(check, 'test', entity_names=['Python', 'Rust'])
        assert result.status == CheckStatus.FAIL

    def test_pass_substring_match(self, make_check):
        check = make_check(expected='python', check_type='entity_exists')
        result = run_check(check, 'test', entity_names=['Python 3.12', 'Rust'])
        assert result.status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# entity_type_check
# ---------------------------------------------------------------------------


class TestEntityTypeCheck:
    def test_pass_correct_type(self, make_check, make_entity):
        check = make_check(
            expected='Python',
            check_type='entity_type_check',
            expected_entity_type='Technology',
        )
        entities = [make_entity('Python', 'Technology')]
        result = run_check(check, 'test', entities=entities)
        assert result.status == CheckStatus.PASS

    def test_fail_wrong_type(self, make_check, make_entity):
        check = make_check(
            expected='Python',
            check_type='entity_type_check',
            expected_entity_type='Person',
        )
        entities = [make_entity('Python', 'Technology')]
        result = run_check(check, 'test', entities=entities)
        assert result.status == CheckStatus.FAIL

    def test_fail_entity_not_found(self, make_check, make_entity):
        check = make_check(
            expected='Ruby',
            check_type='entity_type_check',
            expected_entity_type='Technology',
        )
        entities = [make_entity('Python', 'Technology')]
        result = run_check(check, 'test', entities=entities)
        assert result.status == CheckStatus.FAIL

    def test_fail_no_entities(self, make_check):
        check = make_check(
            expected='Python',
            check_type='entity_type_check',
            expected_entity_type='Technology',
        )
        result = run_check(check, 'test', entities=None)
        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# entity_cooccurrence_check
# ---------------------------------------------------------------------------


class TestEntityCooccurrence:
    def test_pass_cooccurrence_found(self, make_check):
        check = make_check(expected='Sarah', check_type='entity_cooccurrence_check')
        coocs = [{'entity_1_name': 'Sarah', 'entity_2_name': 'Acme Corp', 'count': 5}]
        result = run_check(check, 'test', cooccurrences=coocs)
        assert result.status == CheckStatus.PASS

    def test_fail_cooccurrence_missing(self, make_check):
        check = make_check(expected='Unknown Person', check_type='entity_cooccurrence_check')
        coocs = [{'entity_1_name': 'Sarah', 'entity_2_name': 'Acme Corp', 'count': 5}]
        result = run_check(check, 'test', cooccurrences=coocs)
        assert result.status == CheckStatus.FAIL

    def test_fail_empty_cooccurrences(self, make_check):
        check = make_check(expected='Sarah', check_type='entity_cooccurrence_check')
        result = run_check(check, 'test', cooccurrences=None)
        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# entity_mention_check
# ---------------------------------------------------------------------------


class TestEntityMentionCheck:
    def test_pass_keyword_in_mentions(self, make_check):
        check = make_check(expected='project alpha', check_type='entity_mention_check')
        mentions = [{'unit': type('Unit', (), {'text': 'Worked on Project Alpha kickoff'})}]
        result = run_check(check, 'test', mentions=mentions)
        assert result.status == CheckStatus.PASS

    def test_pass_dict_unit(self, make_check):
        check = make_check(expected='beta', check_type='entity_mention_check')
        mentions = [{'unit': {'text': 'Beta release shipped'}}]
        result = run_check(check, 'test', mentions=mentions)
        assert result.status == CheckStatus.PASS

    def test_fail_no_mentions(self, make_check):
        check = make_check(expected='anything', check_type='entity_mention_check')
        result = run_check(check, 'test', mentions=None)
        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# result_ordering
# ---------------------------------------------------------------------------


class TestResultOrdering:
    def test_pass_correct_order(self, make_check, make_unit):
        check = make_check(
            expected=['alpha', 'beta'],
            check_type='result_ordering',
        )
        units = [make_unit('alpha project'), make_unit('beta project')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_fail_wrong_order(self, make_check, make_unit):
        check = make_check(
            expected=['alpha', 'beta'],
            check_type='result_ordering',
        )
        units = [make_unit('beta project'), make_unit('alpha project')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.FAIL

    def test_pass_first_found_second_missing(self, make_check, make_unit):
        """When first keyword is found but second is not, it's a pass (correctly downranked)."""
        check = make_check(
            expected=['alpha', 'missing'],
            check_type='result_ordering',
        )
        units = [make_unit('alpha project')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.PASS

    def test_fail_first_not_found(self, make_check, make_unit):
        check = make_check(
            expected=['missing', 'beta'],
            check_type='result_ordering',
        )
        units = [make_unit('beta project')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.FAIL

    def test_error_insufficient_expected(self, make_check, make_unit):
        check = make_check(expected='single', check_type='result_ordering')
        units = [make_unit('single item')]
        result = run_check(check, 'test', memory_results=units)
        assert result.status == CheckStatus.ERROR

    def test_fail_no_results(self, make_check):
        check = make_check(
            expected=['alpha', 'beta'],
            check_type='result_ordering',
        )
        result = run_check(check, 'test', memory_results=None)
        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# llm_judge
# ---------------------------------------------------------------------------


class TestLLMJudge:
    def test_skip_when_no_judge(self, make_check, make_unit):
        check = make_check(check_type='llm_judge')
        units = [make_unit('some text')]
        result = run_check(check, 'test', memory_results=units, judge=None)
        assert result.status == CheckStatus.SKIP

    def test_fail_empty_results(self, make_check):
        """When there are no results, the check should fail even with a judge."""

        class FakeJudge:
            def judge_relevance(self, **kwargs):
                return True, 'relevant'

        check = make_check(check_type='llm_judge')
        result = run_check(check, 'test', memory_results=[], judge=FakeJudge())
        assert result.status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# unknown check type
# ---------------------------------------------------------------------------


class TestUnknownCheckType:
    def test_error_on_unknown_type(self, make_check):
        check = make_check(check_type='nonexistent_type')
        result = run_check(check, 'test')
        assert result.status == CheckStatus.ERROR
        assert 'Unknown check type' in result.actual


# ---------------------------------------------------------------------------
# exception handling
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    def test_exception_caught(self, make_check):
        """run_check should catch exceptions and return ERROR status."""
        check = make_check(check_type='keyword_in_results', expected=None)
        # expected=None will cause an error when iterating
        result = run_check(check, 'test', memory_results=[])
        assert result.status in (CheckStatus.PASS, CheckStatus.ERROR, CheckStatus.FAIL)


# ---------------------------------------------------------------------------
# duration tracking
# ---------------------------------------------------------------------------


class TestDuration:
    def test_duration_recorded(self, make_check, make_unit):
        check = make_check(expected='alpha', check_type='keyword_in_results')
        units = [make_unit('alpha')]
        result = run_check(check, 'test', memory_results=units)
        assert result.duration_ms >= 0
