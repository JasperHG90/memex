"""Unit tests for ContentStatus enum and formatted_fact_text with stale prefix."""

from datetime import datetime, timezone

from memex_core.memory.sql_models import ContentStatus, MemoryUnit


class TestContentStatus:
    """Tests for ContentStatus enum."""

    def test_active_value(self) -> None:
        assert ContentStatus.ACTIVE == 'active'
        assert ContentStatus.ACTIVE.value == 'active'

    def test_stale_value(self) -> None:
        assert ContentStatus.STALE == 'stale'
        assert ContentStatus.STALE.value == 'stale'


class TestFormattedFactTextStalePrefix:
    """Tests for [STALE] prefix in formatted_fact_text."""

    def test_active_no_prefix(self) -> None:
        """Active memory units have no [STALE] prefix."""
        mu = MemoryUnit(
            text='Some fact',
            fact_type='world',
            status=ContentStatus.ACTIVE,
            event_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
            occurred_start=datetime(2024, 1, 15, tzinfo=timezone.utc),
            embedding=[0.0] * 384,
        )
        assert mu.formatted_fact_text == '[2024-01-15] Some fact'

    def test_stale_has_prefix(self) -> None:
        """Stale memory units have [STALE] prefix."""
        mu = MemoryUnit(
            text='Old fact',
            fact_type='world',
            status=ContentStatus.STALE,
            event_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
            occurred_start=datetime(2024, 1, 15, tzinfo=timezone.utc),
            embedding=[0.0] * 384,
        )
        assert mu.formatted_fact_text == '[STALE] [2024-01-15] Old fact'

    def test_stale_with_citations(self) -> None:
        """Stale prefix works correctly with citations."""
        mu = MemoryUnit(
            text='Cited fact',
            fact_type='world',
            status=ContentStatus.STALE,
            event_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
            occurred_start=datetime(2024, 1, 15, tzinfo=timezone.utc),
            embedding=[0.0] * 384,
            unit_metadata={
                'citations': [
                    {'text': 'Evidence text', 'date': '2024-01-10'},
                ]
            },
        )
        result = mu.formatted_fact_text
        assert result.startswith('[STALE] [2024-01-15] Cited fact')
        assert '  - [2024-01-10] Evidence text' in result
