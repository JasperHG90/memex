"""Tests for _apply_confidence_penalty in RetrievalEngine."""

from datetime import datetime, timezone
from uuid import uuid4

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import CONTRADICTION_THRESHOLD, MemoryUnit
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


def test_contradicted_units_demoted_to_end():
    """Contradicted opinions (confidence < 0.3) should be moved to the end."""
    confident = _make_unit('confident', confidence_alpha=8.0, confidence_beta=2.0)  # 0.8
    contradicted = _make_unit('contradicted', confidence_alpha=1.0, confidence_beta=9.0)  # 0.1
    neutral = _make_unit('neutral')  # None confidence

    result = RetrievalEngine._apply_confidence_penalty([contradicted, confident, neutral])

    assert result == [confident, neutral, contradicted]


def test_no_contradicted_units_preserves_order():
    """When no units are contradicted, order is preserved."""
    a = _make_unit('a', confidence_alpha=7.0, confidence_beta=3.0)  # 0.7
    b = _make_unit('b')  # None
    c = _make_unit('c', confidence_alpha=5.0, confidence_beta=5.0)  # 0.5

    result = RetrievalEngine._apply_confidence_penalty([a, b, c])

    assert result == [a, b, c]


def test_all_contradicted_preserves_relative_order():
    """When all units are contradicted, relative order is preserved."""
    a = _make_unit('a', confidence_alpha=1.0, confidence_beta=9.0)  # 0.1
    b = _make_unit('b', confidence_alpha=2.0, confidence_beta=8.0)  # 0.2

    result = RetrievalEngine._apply_confidence_penalty([a, b])

    assert result == [a, b]


def test_empty_list():
    """Empty input returns empty output."""
    assert RetrievalEngine._apply_confidence_penalty([]) == []


def test_threshold_boundary():
    """Unit at exactly the threshold should NOT be contradicted."""
    at_threshold = _make_unit(
        'at_threshold',
        confidence_alpha=CONTRADICTION_THRESHOLD,
        confidence_beta=1.0 - CONTRADICTION_THRESHOLD,
    )

    result = RetrievalEngine._apply_confidence_penalty([at_threshold])

    # confidence_score = 0.3 / 1.0 = 0.3, which is NOT < 0.3
    assert result == [at_threshold]
