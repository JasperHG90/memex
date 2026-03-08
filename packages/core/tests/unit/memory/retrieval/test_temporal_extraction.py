"""Tests for NLP-based temporal constraint extraction."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from memex_core.memory.retrieval.temporal_extraction import extract_temporal_constraint


# Fixed reference date for deterministic tests
REF = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


class TestLastWeek:
    """'last week' should return ~7-day range ending near now."""

    def test_returns_range(self):
        result = extract_temporal_constraint('what happened last week', reference_date=REF)
        assert result is not None
        start, end = result
        assert start < end
        # Should span roughly 7 days
        delta = end - start
        assert 6 <= delta.days <= 8

    def test_end_near_reference(self):
        result = extract_temporal_constraint('what happened last week', reference_date=REF)
        assert result is not None
        _start, end = result
        # End should be on or near the reference date
        assert abs((end.date() - REF.date()).days) <= 1


class TestInMonth:
    """'in March 2024' should return March 1-31, 2024."""

    def test_march_2024(self):
        result = extract_temporal_constraint('in March 2024', reference_date=REF)
        assert result is not None
        start, end = result
        assert start.year == 2024
        assert start.month == 3
        assert start.day == 1
        assert end.year == 2024
        assert end.month == 3
        assert end.day == 31

    def test_month_without_year(self):
        """'in March' without year should default to reference year."""
        result = extract_temporal_constraint('what happened in March', reference_date=REF)
        assert result is not None
        start, end = result
        assert start.month == 3
        assert start.year == REF.year
        assert end.month == 3

    def test_february_leap_year(self):
        """February in a leap year should end on the 29th."""
        ref = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = extract_temporal_constraint('in February 2024', reference_date=ref)
        assert result is not None
        _start, end = result
        assert end.day == 29

    def test_february_non_leap_year(self):
        """February in a non-leap year should end on the 28th."""
        ref = datetime(2023, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = extract_temporal_constraint('in February 2023', reference_date=ref)
        assert result is not None
        _start, end = result
        assert end.day == 28


class TestYesterday:
    """'yesterday' should return yesterday's full day."""

    def test_returns_full_day(self):
        result = extract_temporal_constraint('what happened yesterday', reference_date=REF)
        assert result is not None
        start, end = result
        yesterday = REF - timedelta(days=1)
        assert start.date() == yesterday.date()
        assert end.date() == yesterday.date()
        assert start.hour == 0
        assert start.minute == 0
        assert end.hour == 23
        assert end.minute == 59


class TestDaysAgo:
    """'3 days ago' should return a range around 3 days ago."""

    def test_three_days_ago(self):
        result = extract_temporal_constraint('what happened 3 days ago', reference_date=REF)
        assert result is not None
        start, end = result
        target = REF - timedelta(days=3)
        assert start.date() == target.date()
        assert end.date() == target.date()

    def test_word_number(self):
        """'five days ago' with word-based number."""
        result = extract_temporal_constraint('tell me about five days ago', reference_date=REF)
        assert result is not None
        start, end = result
        target = REF - timedelta(days=5)
        assert start.date() == target.date()


class TestNoTemporalExpression:
    """Queries without temporal expressions should return None."""

    def test_no_temporal(self):
        result = extract_temporal_constraint('tell me about cats', reference_date=REF)
        assert result is None

    def test_generic_query(self):
        result = extract_temporal_constraint('what is the meaning of life', reference_date=REF)
        assert result is None


class TestEmptyInput:
    """Empty or whitespace-only input should return None."""

    def test_empty_string(self):
        result = extract_temporal_constraint('', reference_date=REF)
        assert result is None

    def test_whitespace_only(self):
        result = extract_temporal_constraint('   ', reference_date=REF)
        assert result is None


class TestReferenceDate:
    """With explicit reference_date, relative expressions use that as reference."""

    def test_custom_reference(self):
        custom_ref = datetime(2023, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        result = extract_temporal_constraint('yesterday', reference_date=custom_ref)
        assert result is not None
        start, _end = result
        expected = custom_ref - timedelta(days=1)
        assert start.date() == expected.date()

    def test_naive_reference_gets_utc(self):
        """A naive reference_date should be treated as UTC."""
        naive_ref = datetime(2024, 3, 1, 12, 0, 0)
        result = extract_temporal_constraint('yesterday', reference_date=naive_ref)
        assert result is not None
        start, _end = result
        assert start.tzinfo is not None


class TestEdgeCases:
    """Edge cases: future dates, ambiguous expressions, year-only."""

    def test_in_year(self):
        """'in 2024' should return full year range."""
        result = extract_temporal_constraint('what happened in 2024', reference_date=REF)
        assert result is not None
        start, end = result
        assert start.year == 2024
        assert start.month == 1
        assert start.day == 1
        assert end.year == 2024
        assert end.month == 12
        assert end.day == 31

    def test_last_month(self):
        """'last month' should return ~30-day range."""
        result = extract_temporal_constraint('events from last month', reference_date=REF)
        assert result is not None
        start, end = result
        delta = end - start
        assert 29 <= delta.days <= 31

    def test_last_year(self):
        """'last year' should return ~365-day range."""
        result = extract_temporal_constraint('what happened last year', reference_date=REF)
        assert result is not None
        start, end = result
        delta = end - start
        assert 364 <= delta.days <= 366

    def test_today(self):
        """'today' should return today's full day."""
        result = extract_temporal_constraint('what happened today', reference_date=REF)
        assert result is not None
        start, end = result
        assert start.date() == REF.date()
        assert end.date() == REF.date()

    def test_timezone_preserved(self):
        """Returned dates should be timezone-aware."""
        result = extract_temporal_constraint('yesterday', reference_date=REF)
        assert result is not None
        start, end = result
        assert start.tzinfo is not None
        assert end.tzinfo is not None

    def test_start_before_end(self):
        """start_date should always be before end_date."""
        queries = [
            'last week',
            'yesterday',
            '3 days ago',
            'in March 2024',
            'last month',
            'in 2024',
            '2 months ago',
            '1 year ago',
        ]
        for q in queries:
            result = extract_temporal_constraint(q, reference_date=REF)
            if result is not None:
                start, end = result
                assert start <= end, f'Failed for query: {q}'


class TestMonthsAgo:
    """'N months ago' should return a single calendar month window."""

    def test_two_months_ago(self):
        """'2 months ago' from June 2024 should return April 2024."""
        result = extract_temporal_constraint('what happened 2 months ago', reference_date=REF)
        assert result is not None
        start, end = result
        assert start.year == 2024
        assert start.month == 4
        assert start.day == 1
        assert end.year == 2024
        assert end.month == 4
        assert end.day == 30

    def test_months_ago_crosses_year_boundary(self):
        """'8 months ago' from March 2024 should return July 2023."""
        ref = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = extract_temporal_constraint('what happened 8 months ago', reference_date=ref)
        assert result is not None
        start, end = result
        assert start.year == 2023
        assert start.month == 7
        assert start.day == 1
        assert end.month == 7
        assert end.day == 31


class TestYearsAgo:
    """'N years ago' should return a quarter (3 months) centered on the target."""

    def test_one_year_ago(self):
        """'1 year ago' from June 2024 should return May-Jul 2023."""
        result = extract_temporal_constraint('what happened 1 year ago', reference_date=REF)
        assert result is not None
        start, end = result
        assert start.year == 2023
        assert start.month == 5
        assert start.day == 1
        assert end.year == 2023
        assert end.month == 7
        assert end.day == 31

    def test_years_ago_narrower_than_before(self):
        """'1 year ago' window should be ~3 months, not ~364 days."""
        result = extract_temporal_constraint('what happened 1 year ago', reference_date=REF)
        assert result is not None
        start, end = result
        delta = end - start
        # 3 months is roughly 89-92 days
        assert delta.days < 100

    def test_years_ago_january_wraps(self):
        """'1 year ago' from January should wrap start month to previous year."""
        ref = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = extract_temporal_constraint('what happened 1 year ago', reference_date=ref)
        assert result is not None
        start, end = result
        # Target: Jan 2023, window: Dec 2022 - Feb 2023
        assert start.year == 2022
        assert start.month == 12
        assert end.year == 2023
        assert end.month == 2


class TestDateparserFallback:
    """Queries with temporal triggers that don't match any regex should fall back to dateparser."""

    def test_dateparser_fallback_called(self):
        """A temporal trigger that doesn't match regex patterns should try dateparser."""
        # 'during January 2024' has a temporal trigger ('during ... january')
        # but doesn't match our regex patterns (which use 'in <month>')
        result = extract_temporal_constraint('during January 2024', reference_date=REF)
        # dateparser should parse this; if not installed, returns None
        # Either way, the function should not raise
        if result is not None:
            start, end = result
            assert start <= end

    @patch('memex_core.memory.retrieval.temporal_extraction._try_dateparser')
    def test_dateparser_fallback_invoked_when_regex_fails(self, mock_dateparser):
        """When regex patterns fail but trigger matches, dateparser is invoked."""
        mock_dateparser.return_value = None
        # 'since January 2024' has a temporal trigger but no regex match
        extract_temporal_constraint('since January 2024', reference_date=REF)
        mock_dateparser.assert_called_once()


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


class TestMultipleTemporalExpressions:
    """Queries with multiple temporal expressions should use the first match."""

    def test_first_expression_wins(self):
        """When query has multiple temporal cues, result should be valid."""
        result = extract_temporal_constraint(
            'yesterday I recalled last week events', reference_date=REF
        )
        assert result is not None
        start, end = result
        assert start < end
        # 'yesterday' appears first and should match
        yesterday = REF - timedelta(days=1)
        assert start.date() == yesterday.date()


class TestVeryLongQuery:
    """Very long queries should not crash or hang."""

    def test_long_query_with_temporal(self):
        """A long query containing a temporal expression still extracts it."""
        filler = 'some random words about nothing important ' * 50
        query = f'{filler} what happened yesterday {filler}'
        result = extract_temporal_constraint(query, reference_date=REF)
        assert result is not None
        start, end = result
        yesterday = REF - timedelta(days=1)
        assert start.date() == yesterday.date()

    def test_long_query_without_temporal(self):
        """A long query without temporal expression returns None."""
        query = 'tell me about cats and dogs ' * 100
        result = extract_temporal_constraint(query, reference_date=REF)
        assert result is None


class TestFutureDates:
    """Future-looking expressions and edge cases."""

    def test_future_reference_date(self):
        """A far-future reference date works for relative expressions."""
        future_ref = datetime(2099, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = extract_temporal_constraint('yesterday', reference_date=future_ref)
        assert result is not None
        start, _end = result
        expected = future_ref - timedelta(days=1)
        assert start.date() == expected.date()

    def test_zero_days_ago_is_today(self):
        """'0 days ago' should return today's range (boundary)."""
        result = extract_temporal_constraint('0 days ago', reference_date=REF)
        if result is not None:
            start, end = result
            assert start.date() == REF.date()


class TestReferenceNone:
    """When reference_date is None, defaults to now(UTC)."""

    def test_none_reference_uses_utc_now(self):
        result = extract_temporal_constraint('yesterday', reference_date=None)
        assert result is not None
        start, end = result
        assert start.tzinfo is not None
        assert start < end


class TestTemporalExtractionDisabled:
    """Test that temporal_extraction_enabled=False skips extraction in engine."""

    @pytest.mark.asyncio
    async def test_temporal_extraction_disabled_skips_extraction(self):
        """When temporal_extraction_enabled=False, no date filters are injected."""
        from memex_common.config import RetrievalConfig
        from memex_core.memory.retrieval.engine import RetrievalEngine
        from memex_core.memory.retrieval.models import RetrievalRequest
        from sqlmodel.ext.asyncio.session import AsyncSession

        config = RetrievalConfig(temporal_extraction_enabled=False)
        mock_embedder = MagicMock()
        mock_vec = MagicMock()
        mock_vec.tolist.return_value = [0.1] * 384
        mock_embedder.encode.return_value = [mock_vec]

        engine = RetrievalEngine(embedder=mock_embedder, retrieval_config=config)

        session = MagicMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.all.return_value = []
        session.exec.return_value = mock_result

        original_filters = {'custom': 'value'}
        request = RetrievalRequest(
            query='what happened last week',
            filters=original_filters,
        )

        await engine.retrieve(session, request)

        # Original filters should NOT be mutated (no start_date/end_date injected)
        assert 'start_date' not in original_filters
        assert 'end_date' not in original_filters
