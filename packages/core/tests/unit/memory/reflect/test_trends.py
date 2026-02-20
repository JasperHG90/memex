from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from memex_core.memory.sql_models import Trend
from memex_core.memory.reflect.trends import compute_trend


@dataclass
class MockEvidence:
    timestamp: datetime


def test_trend_no_evidence():
    assert compute_trend([], now=datetime.now(timezone.utc)) == Trend.STALE


def test_trend_stale():
    """Evidence only exists in the past (> 30 days ago)."""
    now = datetime.now(timezone.utc)
    evidence = [
        MockEvidence(timestamp=now - timedelta(days=40)),
        MockEvidence(timestamp=now - timedelta(days=100)),
    ]
    assert compute_trend(evidence, now=now) == Trend.STALE


def test_trend_new():
    """Evidence only exists recently (< 30 days ago)."""
    now = datetime.now(timezone.utc)
    evidence = [
        MockEvidence(timestamp=now - timedelta(days=1)),
        MockEvidence(timestamp=now - timedelta(days=10)),
    ]
    assert compute_trend(evidence, now=now) == Trend.NEW


def test_trend_strengthening():
    """High density recent evidence compared to older evidence."""
    now = datetime.now(timezone.utc)
    # 5 recent items (high density)
    recent = [MockEvidence(timestamp=now - timedelta(days=i)) for i in range(1, 6)]
    # 1 old item (low density)
    old = [MockEvidence(timestamp=now - timedelta(days=60))]

    evidence = recent + old
    assert compute_trend(evidence, now=now) == Trend.STRENGTHENING


def test_trend_weakening():
    """Low density recent evidence compared to older evidence."""
    now = datetime.now(timezone.utc)
    # 1 recent item
    recent = [MockEvidence(timestamp=now - timedelta(days=1))]
    # 10 old items
    old = [MockEvidence(timestamp=now - timedelta(days=40 + i)) for i in range(10)]

    evidence = recent + old
    assert compute_trend(evidence, now=now) == Trend.WEAKENING


def test_trend_stable():
    """Roughly equal density."""
    now = datetime.now(timezone.utc)
    # 2 recent items
    recent = [
        MockEvidence(timestamp=now - timedelta(days=5)),
        MockEvidence(timestamp=now - timedelta(days=10)),
    ]
    # 4 old items (over a longer 60 day span, so density is roughly similar)
    old = [
        MockEvidence(timestamp=now - timedelta(days=40)),
        MockEvidence(timestamp=now - timedelta(days=50)),
        MockEvidence(timestamp=now - timedelta(days=60)),
        MockEvidence(timestamp=now - timedelta(days=70)),
    ]

    # recent_density = 2/30 = 0.066
    # old_density = 4/60 = 0.066
    # ratio = 1.0

    evidence = recent + old
    assert compute_trend(evidence, now=now) == Trend.STABLE
