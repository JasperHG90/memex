"""Tests for _apply_confidence_weighting in RetrievalEngine."""

from datetime import datetime, timezone
from uuid import uuid4

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryUnit
from memex_common.types import FactTypes


def _make_unit(
    text: str = 'test',
    confidence_alpha: float | None = None,
    confidence_beta: float | None = None,
) -> MemoryUnit:
    """Create a minimal MemoryUnit for testing."""
    return MemoryUnit(
        id=uuid4(),
        note_id=uuid4(),
        text=text,
        fact_type=FactTypes.OPINION,
        vault_id=uuid4(),
        confidence_alpha=confidence_alpha,
        confidence_beta=confidence_beta,
        occurred_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_low_confidence_demoted_proportionally():
    """Position 1 with confidence=0.1 should be demoted below position 2 with confidence=0.9."""
    low_conf = _make_unit('low', confidence_alpha=1.0, confidence_beta=9.0)  # ~0.1
    high_conf = _make_unit('high', confidence_alpha=9.0, confidence_beta=1.0)  # ~0.9

    result = RetrievalEngine._apply_confidence_weighting([low_conf, high_conf])

    assert result[0] is high_conf
    assert result[1] is low_conf


def test_none_confidence_treated_as_full():
    """Non-opinion units (confidence=None) should stay in place with factor 1.0."""
    fact = _make_unit('fact')  # None confidence
    opinion = _make_unit('opinion', confidence_alpha=9.0, confidence_beta=1.0)  # ~0.9

    result = RetrievalEngine._apply_confidence_weighting([fact, opinion])

    # fact at pos 0: position_score=1.0, factor=1.0, weighted=1.0
    # opinion at pos 1: position_score=0.5, factor=0.93, weighted=0.465
    assert result[0] is fact


def test_empty_list():
    """Empty input returns empty output."""
    assert RetrievalEngine._apply_confidence_weighting([]) == []


def test_all_high_confidence_preserves_order():
    """When all opinions are confident, order should be mostly preserved."""
    a = _make_unit('a', confidence_alpha=8.0, confidence_beta=2.0)  # 0.8
    b = _make_unit('b', confidence_alpha=7.0, confidence_beta=3.0)  # 0.7
    c = _make_unit('c', confidence_alpha=9.0, confidence_beta=1.0)  # 0.9

    result = RetrievalEngine._apply_confidence_weighting([a, b, c])

    # With similar confidence, position score dominates
    assert result[0] is a
