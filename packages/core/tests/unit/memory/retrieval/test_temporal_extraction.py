"""Tests for NLP-based temporal constraint extraction."""

from datetime import datetime, timedelta, timezone


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
        ]
        for q in queries:
            result = extract_temporal_constraint(q, reference_date=REF)
            if result is not None:
                start, end = result
                assert start <= end, f'Failed for query: {q}'
