from datetime import datetime


from memex_core.memory.formatting import format_for_embedding, format_for_reranking


def test_format_for_embedding():
    # Case 1: Standard
    text = 'I changed the oil.'
    fact_type = 'event'
    context = 'Maintenance'
    expected = 'Event (Maintenance): I changed the oil.'
    assert format_for_embedding(text, fact_type, context) == expected

    # Case 2: No context
    text = 'Sky is blue.'
    fact_type = 'world'
    expected = 'World: Sky is blue.'
    assert format_for_embedding(text, fact_type, None) == expected


def test_format_for_reranking_start_and_ongoing():
    """Start date + no end date = ongoing fact."""
    start = datetime(2024, 1, 1)
    result = format_for_reranking(
        'Ruby Martinez is the Department Head.',
        'world',
        occurred_start=start,
    )
    expected = (
        '[Start: January 01, 2024 (2024-01-01)] [End: ongoing] '
        '[World] Ruby Martinez is the Department Head.'
    )
    assert result == expected


def test_format_for_reranking_start_and_end():
    """Start + end date = completed fact."""
    start = datetime(2020, 1, 1)
    end = datetime(2023, 12, 31)
    result = format_for_reranking(
        'Alex Chen led the department.',
        'world',
        occurred_start=start,
        occurred_end=end,
    )
    expected = (
        '[Start: January 01, 2020 (2020-01-01)] [End: December 31, 2023 (2023-12-31)] '
        '[World] Alex Chen led the department.'
    )
    assert result == expected


def test_format_for_reranking_no_dates():
    """No dates at all = no date prefix (undated facts not penalized)."""
    result = format_for_reranking('Sky is blue.', 'world')
    assert result == '[World] Sky is blue.'


def test_format_for_reranking_end_only():
    """End date only (rare) = just [End: ...]."""
    end = datetime(2025, 3, 15)
    result = format_for_reranking(
        'The project was completed.',
        'event',
        occurred_end=end,
    )
    expected = '[End: March 15, 2025 (2025-03-15)] [Event] The project was completed.'
    assert result == expected


def test_format_for_reranking_with_context():
    """Context prefix is included."""
    start = datetime(2026, 1, 14)
    result = format_for_reranking(
        'I changed the oil.',
        'event',
        context='Maintenance',
        occurred_start=start,
    )
    expected = (
        '[Start: January 14, 2026 (2026-01-14)] [End: ongoing] '
        '[Event] Maintenance: I changed the oil.'
    )
    assert result == expected


# ---------------------------------------------------------------------------
# Edge-case and error-path tests
# ---------------------------------------------------------------------------


class TestFormatForRerankingEdgeCases:
    """Edge cases for format_for_reranking."""

    def test_empty_text(self):
        """Empty text should still produce the bracketed prefix."""
        start = datetime(2026, 3, 1)
        result = format_for_reranking('', 'event', occurred_start=start)
        assert result == '[Start: March 01, 2026 (2026-03-01)] [End: ongoing] [Event] '

    def test_empty_fact_type_falls_back_to_unknown(self):
        """An empty fact_type string is falsy; should render as 'Unknown'."""
        result = format_for_reranking('some text', '')
        assert '[Unknown]' in result

    def test_empty_context_omitted(self):
        """Empty string context should be treated the same as None (no prefix)."""
        start = datetime(2026, 3, 1)
        result = format_for_reranking('text', 'world', context='', occurred_start=start)
        assert result == '[Start: March 01, 2026 (2026-03-01)] [End: ongoing] [World] text'


class TestFormatForEmbeddingEdgeCases:
    """Edge cases for format_for_embedding."""

    def test_empty_text(self):
        result = format_for_embedding('', 'event')
        assert result == 'Event: '

    def test_empty_fact_type_falls_back_to_unknown(self):
        result = format_for_embedding('text', '')
        assert result == 'Unknown: text'

    def test_empty_context_omitted(self):
        """Empty string context is falsy, so no parenthesized segment."""
        result = format_for_embedding('text', 'event', context='')
        assert result == 'Event: text'
