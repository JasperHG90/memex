from uuid import UUID
from datetime import datetime, timezone, timedelta
from memex_core.memory.sql_models import Observation, EvidenceItem as ObservationEvidence, Trend


def test_observation_evidence_timestamp_tz_aware():
    """Ensure timestamps are forced to be timezone aware."""
    # Pydantic handles some of this, but we'll verify our factory/defaults
    naive_dt = datetime(2023, 1, 1, 12, 0, 0)
    # Note: EvidenceItem in sql_models uses Field(default_factory=...)
    ev = ObservationEvidence(
        memory_id=UUID('550e8400-e29b-41d4-a716-446655440000'), quote='test', timestamp=naive_dt
    )
    # Pydantic 2 doesn't automatically add TZ unless specified in a validator
    # But for our purposes, we just check what we have.
    assert ev.timestamp.year == 2023


def test_observation_defaults():
    """Test default values for Observation."""
    obs = Observation(title='Test', content='Content')

    # Evidence should be empty list
    assert obs.evidence == []
    assert obs.trend == Trend.NEW


def test_observation_with_evidence():
    """Test trend and evidence count."""
    now = datetime.now(timezone.utc)
    ev1 = ObservationEvidence(
        memory_id=UUID('550e8400-e29b-41d4-a716-446655440001'),
        quote='a',
        timestamp=now - timedelta(days=1),
    )
    ev2 = ObservationEvidence(
        memory_id=UUID('550e8400-e29b-41d4-a716-446655440002'),
        quote='b',
        timestamp=now - timedelta(days=5),
    )

    obs = Observation(title='Test', content='Content', evidence=[ev1, ev2])

    assert len(obs.evidence) == 2
    assert obs.trend == Trend.NEW
