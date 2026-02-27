"""Tests for [CONTRADICTED] prefix in MemoryUnit.formatted_fact_text."""

from datetime import datetime, timezone
from uuid import uuid4

from memex_core.memory.sql_models import CONTRADICTION_THRESHOLD, MemoryUnit
from memex_common.types import FactTypes


def _make_unit(
    text: str = 'qmd is a markdown format',
    confidence_alpha: float | None = None,
    confidence_beta: float | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=uuid4(),
        note_id=uuid4(),
        text=text,
        fact_type=FactTypes.OPINION,
        vault_id=uuid4(),
        confidence_alpha=confidence_alpha,
        confidence_beta=confidence_beta,
        occurred_start=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )


def test_contradicted_prefix_when_low_confidence():
    """Low confidence units should have [CONTRADICTED] prefix."""
    unit = _make_unit(confidence_alpha=1.0, confidence_beta=9.0)  # 0.1

    result = unit.formatted_fact_text

    assert result.startswith('[CONTRADICTED]')
    assert '[2026-01-15]' in result
    assert 'qmd is a markdown format' in result


def test_no_contradicted_prefix_when_high_confidence():
    """High confidence units should NOT have [CONTRADICTED] prefix."""
    unit = _make_unit(confidence_alpha=8.0, confidence_beta=2.0)  # 0.8

    result = unit.formatted_fact_text

    assert '[CONTRADICTED]' not in result
    assert '[2026-01-15]' in result


def test_no_contradicted_prefix_when_no_confidence():
    """Units without confidence (world/experience facts) should NOT have prefix."""
    unit = _make_unit()

    result = unit.formatted_fact_text

    assert '[CONTRADICTED]' not in result


def test_contradicted_and_stale_both_present():
    """Both [STALE] and [CONTRADICTED] should appear when applicable."""
    unit = _make_unit(confidence_alpha=1.0, confidence_beta=9.0)
    unit.status = 'stale'

    result = unit.formatted_fact_text

    assert '[STALE]' in result
    assert '[CONTRADICTED]' in result


def test_threshold_boundary_not_contradicted():
    """At exactly the threshold, should NOT be marked contradicted."""
    unit = _make_unit(
        confidence_alpha=CONTRADICTION_THRESHOLD,
        confidence_beta=1.0 - CONTRADICTION_THRESHOLD,
    )

    result = unit.formatted_fact_text

    assert '[CONTRADICTED]' not in result
