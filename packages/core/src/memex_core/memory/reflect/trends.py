"""
Logic for computing observation trends based on evidence timestamps.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from memex_core.memory.sql_models import Trend


def compute_trend(
    evidence: list[Any],  # Typed as Any to avoid circular import issues at runtime
    now: datetime | None = None,
    recent_days: int = 30,
    old_days: int = 90,
) -> Trend:
    """Compute the trend for an observation based on evidence timestamps.

    Args:
        evidence: List of evidence items with timestamps (ObservationEvidence)
        now: Reference time for calculations (defaults to current UTC time)
        recent_days: Number of days to consider "recent" (default 30)
        old_days: Number of days to consider "old" (default 90)

    Returns:
        Computed Trend enum value
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Ensure now is timezone-aware
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if not evidence:
        return Trend.STALE

    recent_cutoff = now - timedelta(days=recent_days)
    old_cutoff = now - timedelta(days=old_days)

    # Helper to access timestamp regardless of object type (dict or object)
    def get_ts(item: Any) -> datetime:
        if isinstance(item, dict):
            ts = item.get('timestamp')
        else:
            ts = getattr(item, 'timestamp', None)

        if ts is None:
            return now  # Fallback

        if isinstance(ts, str):
            dt_obj = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            if dt_obj.tzinfo is None:
                return dt_obj.replace(tzinfo=timezone.utc)
            return dt_obj

        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts

    recent = [e for e in evidence if get_ts(e) > recent_cutoff]
    old = [e for e in evidence if get_ts(e) < old_cutoff]
    middle = [e for e in evidence if old_cutoff <= get_ts(e) <= recent_cutoff]

    # No recent evidence = stale
    if not recent:
        return Trend.STALE

    # All evidence is recent = new
    if not old and not middle:
        return Trend.NEW

    # Compare density (evidence per day)
    recent_density = len(recent) / recent_days if recent_days > 0 else 0
    older_period = old_days - recent_days
    older_density = (len(old) + len(middle)) / older_period if older_period > 0 else 0

    # Avoid division by zero
    if older_density == 0:
        return Trend.NEW

    ratio = recent_density / older_density

    if ratio > 1.5:
        return Trend.STRENGTHENING
    elif ratio < 0.5:
        return Trend.WEAKENING
    else:
        return Trend.STABLE


def verify_evidence_quotes(
    observation: Any,  # Typed as Any (Observation)
    memories: dict[str, str],
) -> tuple[bool, list[str]]:
    """Verify that all evidence quotes exist in the referenced memories.

    Args:
        observation: The observation to verify
        memories: Dict mapping memory_id to memory content

    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors = []

    for evidence in observation.evidence:
        memory_content = memories.get(evidence.memory_id)
        if memory_content is None:
            errors.append(f'Memory {evidence.memory_id} not found')
            continue

        if evidence.quote not in memory_content:
            # Fuzzy match check could go here if strict match fails
            errors.append(
                f"Quote not found in memory {evidence.memory_id}: '{evidence.quote[:50]}...'"
            )

    return len(errors) == 0, errors
