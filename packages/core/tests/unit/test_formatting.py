from datetime import datetime

import pytest

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


def test_format_for_reranking():
    dt = datetime(2026, 1, 14)
    text = 'I changed the oil.'
    fact_type = 'event'
    context = 'Maintenance'

    # Expected: [Date: January 14, 2026 (2026-01-14)] [Event] Maintenance: I changed the oil.
    expected = '[Date: January 14, 2026 (2026-01-14)] [Event] Maintenance: I changed the oil.'
    assert format_for_reranking(text, dt, fact_type, context) == expected

    # Case 2: No context
    text = 'Sky is blue.'
    fact_type = 'world'
    expected = '[Date: January 14, 2026 (2026-01-14)] [World] Sky is blue.'
    assert format_for_reranking(text, dt, fact_type, None) == expected


# ---------------------------------------------------------------------------
# Edge-case and error-path tests (AUDIT-031 sub-item 1)
# ---------------------------------------------------------------------------


class TestFormatForRerankingEdgeCases:
    """Edge cases for format_for_reranking: empty string, falsy fact_type, None date."""

    def test_empty_text(self):
        """Empty text should still produce the bracketed prefix."""
        dt_val = datetime(2026, 3, 1)
        result = format_for_reranking('', dt_val, 'event')
        assert result == '[Date: March 01, 2026 (2026-03-01)] [Event] '

    def test_empty_fact_type_falls_back_to_unknown(self):
        """An empty fact_type string is falsy; should render as 'Unknown'."""
        dt_val = datetime(2026, 3, 1)
        result = format_for_reranking('some text', dt_val, '')
        assert '[Unknown]' in result

    def test_none_date_raises(self):
        """Passing None as event_date should raise AttributeError."""
        with pytest.raises(AttributeError):
            format_for_reranking('text', None, 'event')  # type: ignore[arg-type]

    def test_empty_context_omitted(self):
        """Empty string context should be treated the same as None (no prefix)."""
        dt_val = datetime(2026, 3, 1)
        result = format_for_reranking('text', dt_val, 'world', context='')
        # Empty context is falsy, so ctx_prefix should be ''
        assert result == '[Date: March 01, 2026 (2026-03-01)] [World] text'


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
