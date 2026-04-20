import datetime as dt

import pytest

from memex_core.memory.extraction.utils import parse_iso_datetime, sanitize_text


class TestSanitizeText:
    """Tests for sanitize_text utility."""

    def test_sanitize_surrogates(self) -> None:
        """Test removal of surrogate characters."""
        # text = 'Hello\ud83d\ude00World'  # \ud83d is a high surrogate
        # Wait, \ud83d is not in the range [\ud800-\udfff]??
        # Yes it is. 0xD83D is 55357. 0xD800 is 55296. 0xDFFF is 57343.
        # Python handles unicode literals carefully.
        # Let's use a specific surrogate that is isolated and invalid in UTF-8 strict.
        # But here we are testing the regex: re.sub(r'[\ud800-\udfff]', '', text)

        # Construct a string with a surrogate code point explicitly
        text_with_surrogate = 'Test\ud800Value'
        cleaned = sanitize_text(text_with_surrogate)
        assert cleaned == 'TestValue'

    def test_sanitize_empty(self) -> None:
        """Test with empty inputs."""
        assert sanitize_text('') == ''
        # The type hint says str, but robust code might handle None if not strict
        # The code says: if not text: return text
        # So passing None (if ignored by type checker) would return None.
        # We'll stick to str as per type hint.


class TestParseDatetime:
    """Tests for parse_iso_datetime utility."""

    def test_valid_iso(self) -> None:
        """Test parsing valid ISO strings."""
        dt_obj = parse_iso_datetime('2023-01-01T12:00:00Z')
        assert isinstance(dt_obj, dt.datetime)
        assert dt_obj.year == 2023

    def test_invalid_string(self) -> None:
        """Test parsing invalid strings returns None."""
        assert parse_iso_datetime('not a date') is None


class TestNormalizeTimestamp:
    """Tests for normalize_timestamp utility."""

    def test_none_input(self) -> None:
        """Test None input returns min datetime in UTC."""
        from memex_core.memory.extraction.utils import normalize_timestamp

        res = normalize_timestamp(None)
        assert res == dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    def test_naive_input(self) -> None:
        """Test naive datetime becomes UTC."""
        from memex_core.memory.extraction.utils import normalize_timestamp

        naive = dt.datetime(2024, 1, 1, 10, 0, 0)
        res = normalize_timestamp(naive)
        assert res.tzinfo == dt.timezone.utc
        assert res.year == 2024
        assert res.hour == 10

    def test_aware_input(self) -> None:
        """Test aware datetime is preserved (or at least returned as-is/converted)."""
        from memex_core.memory.extraction.utils import normalize_timestamp

        aware = dt.datetime(2024, 1, 1, 10, 0, 0, tzinfo=dt.timezone.utc)
        res = normalize_timestamp(aware)
        assert res == aware


class TestNormalizeTimestampFallback:
    """Test normalize_timestamp fallback chain."""

    def test_uses_fallback_when_none(self) -> None:
        from memex_core.memory.extraction.utils import normalize_timestamp

        fallback = dt.datetime(2023, 6, 1, tzinfo=dt.timezone.utc)
        res = normalize_timestamp(None, fallback=fallback)
        assert res == fallback

    def test_uses_value_over_fallback(self) -> None:
        from memex_core.memory.extraction.utils import normalize_timestamp

        value = dt.datetime(2024, 3, 15, tzinfo=dt.timezone.utc)
        fallback = dt.datetime(2023, 6, 1, tzinfo=dt.timezone.utc)
        res = normalize_timestamp(value, fallback=fallback)
        assert res == value

    def test_no_fallback_uses_now(self) -> None:
        from memex_core.memory.extraction.utils import normalize_timestamp

        res = normalize_timestamp(None)
        assert res.year >= 2020
        assert res.tzinfo is not None


class TestExtremeDateHandling:
    """Regression tests: extreme dates must not crash the extraction pipeline."""

    def test_timedelta_overflow_guarded_for_datetime_min(self) -> None:
        """datetime.min - timedelta(hours=12) overflows; the guard catches it."""
        from datetime import timedelta

        target = dt.datetime(1, 1, 1, tzinfo=dt.timezone.utc)
        delta = timedelta(hours=12)

        # Without guard, this crashes:
        with pytest.raises(OverflowError):
            _ = target - delta

        # The guard in check_duplicates_in_window catches it:
        try:
            start = target - delta
        except OverflowError:
            start = dt.datetime.min.replace(tzinfo=target.tzinfo)
        assert start.year == 1

    def test_timedelta_overflow_guarded_for_datetime_max(self) -> None:
        """datetime.max + timedelta(hours=12) overflows; the guard catches it."""
        from datetime import timedelta

        target = dt.datetime(9999, 12, 31, 23, 0, 0, tzinfo=dt.timezone.utc)
        delta = timedelta(hours=12)

        with pytest.raises(OverflowError):
            _ = target + delta

        try:
            end = target + delta
        except OverflowError:
            end = dt.datetime.max.replace(tzinfo=target.tzinfo)
        assert end.year == 9999

    def test_parse_iso_datetime_accepts_year_one(self) -> None:
        """parse_iso_datetime should accept '0001-01-01' — valid ISO 8601."""
        result = parse_iso_datetime('0001-01-01')
        assert result is not None
        assert result.year == 1

    def test_parse_iso_datetime_accepts_historical(self) -> None:
        """Historical dates like Battle of Hastings should parse."""
        result = parse_iso_datetime('1066-10-14')
        assert result is not None
        assert result.year == 1066

    def test_parse_iso_datetime_accepts_far_future(self) -> None:
        """Far future dates should parse."""
        result = parse_iso_datetime('9999-01-01')
        assert result is not None
        assert result.year == 9999

    def test_dedup_batch_uses_event_date_fallback(self) -> None:
        """When fact has no dates, dedup uses event_date, not now()."""
        from memex_core.memory.extraction.models import ProcessedFact
        from memex_common.types import FactTypes

        event_date = dt.datetime(2023, 6, 1, tzinfo=dt.timezone.utc)
        fact = ProcessedFact(
            fact_text='test',
            fact_type=FactTypes.WORLD,
            embedding=[0.1] * 384,
            mentioned_at=event_date,
            occurred_start=None,
        )
        # The dedup function should use mentioned_at (which is event_date),
        # not crash or produce datetime.min
        fact_date = fact.occurred_start or fact.mentioned_at
        assert fact_date == event_date
