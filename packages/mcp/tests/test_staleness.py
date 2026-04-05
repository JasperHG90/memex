"""Unit tests for staleness computation — pure function, no mocks needed."""

from datetime import datetime, timedelta, timezone

from memex_mcp.models import Staleness
from memex_mcp.server import compute_staleness


_NOW = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)


def _date_ago(days: int) -> datetime:
    return _NOW - timedelta(days=days)


class TestComputeStaleness:
    def test_fresh_recent_high_confidence(self):
        result = compute_staleness(
            event_date=_date_ago(2),
            confidence=0.9,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.FRESH

    def test_aging_15_day_old(self):
        result = compute_staleness(
            event_date=_date_ago(15),
            confidence=0.8,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.AGING

    def test_stale_45_day_old(self):
        result = compute_staleness(
            event_date=_date_ago(45),
            confidence=0.9,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.STALE

    def test_stale_low_confidence(self):
        """Recent date but confidence below 0.5 → STALE."""
        result = compute_staleness(
            event_date=_date_ago(5),
            confidence=0.3,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.STALE

    def test_contested_superseded(self):
        """Has superseded_by entries → CONTESTED."""
        result = compute_staleness(
            event_date=_date_ago(2),
            confidence=0.9,
            superseded_by=[
                {'unit_id': '123', 'unit_text': 'newer fact', 'relation': 'contradicts'}
            ],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.CONTESTED

    def test_contested_contradiction_link(self):
        """Has a contradiction-type link → CONTESTED."""
        result = compute_staleness(
            event_date=_date_ago(2),
            confidence=0.9,
            superseded_by=[],
            links=[{'relation': 'contradicts', 'unit_id': '456'}],
            now=_NOW,
        )
        assert result == Staleness.CONTESTED

    def test_contested_weakens_link(self):
        """Has a 'weakens' relation → CONTESTED."""
        result = compute_staleness(
            event_date=_date_ago(2),
            confidence=0.9,
            superseded_by=[],
            links=[{'relation': 'weakens', 'unit_id': '789'}],
            now=_NOW,
        )
        assert result == Staleness.CONTESTED

    def test_contested_takes_precedence_over_time_based(self):
        """Old + contested = CONTESTED, not STALE."""
        result = compute_staleness(
            event_date=_date_ago(45),
            confidence=0.3,
            superseded_by=[{'unit_id': '123', 'unit_text': 'newer', 'relation': 'contradicts'}],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.CONTESTED

    def test_no_event_date_high_confidence_is_aging(self):
        """No date but high confidence → AGING (unknown age), not STALE."""
        result = compute_staleness(
            event_date=None,
            confidence=0.9,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.AGING

    def test_no_event_date_low_confidence_is_stale(self):
        """No date and low confidence → STALE."""
        result = compute_staleness(
            event_date=None,
            confidence=0.3,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.STALE

    def test_fresh_note_no_event_date_not_penalised(self):
        """World fact from a fresh note with no unit-level date should not be STALE.

        The DTO may lack event_date/mentioned_at for world facts. A high
        confidence score indicates the fact is still trustworthy, so it
        should be AGING (unknown age) rather than STALE.
        """
        result = compute_staleness(
            event_date=None,
            confidence=0.95,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.AGING

    def test_exactly_7_days_is_aging(self):
        result = compute_staleness(
            event_date=_date_ago(7),
            confidence=0.8,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.AGING

    def test_exactly_30_days_is_aging(self):
        result = compute_staleness(
            event_date=_date_ago(30),
            confidence=0.8,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.AGING

    def test_31_days_is_stale(self):
        result = compute_staleness(
            event_date=_date_ago(31),
            confidence=0.8,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.STALE

    def test_moderate_confidence_recent_is_aging(self):
        """Recent but confidence between 0.5 and 0.7 → AGING."""
        result = compute_staleness(
            event_date=_date_ago(2),
            confidence=0.6,
            superseded_by=[],
            links=[],
            now=_NOW,
        )
        assert result == Staleness.AGING

    def test_non_contradiction_link_not_contested(self):
        """Links with non-contradiction relations should not trigger CONTESTED."""
        result = compute_staleness(
            event_date=_date_ago(2),
            confidence=0.9,
            superseded_by=[],
            links=[{'relation': 'reinforces', 'unit_id': '123'}],
            now=_NOW,
        )
        assert result == Staleness.FRESH
