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
