import datetime as dt


from memex_core.memory.sql_models import TokenUsage
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


class TestTokenUsage:
    """Tests for TokenUsage model."""

    def test_addition(self) -> None:
        """Test adding two TokenUsage objects."""
        u1 = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        u2 = TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30)
        u3 = u1 + u2

        assert u3.input_tokens == 30
        assert u3.output_tokens == 15
        assert u3.total_tokens == 45
        # Ensure originals are unchanged
        assert u1.input_tokens == 10

    def test_inplace_addition(self) -> None:
        """Test inplace addition."""
        u1 = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        u2 = TokenUsage(input_tokens=20, output_tokens=10, total_tokens=30)
        u1 += u2

        assert u1.input_tokens == 30
        assert u1.output_tokens == 15
        assert u1.total_tokens == 45
