"""Tests for confidence awareness in reflection."""

from datetime import datetime, timezone
from uuid import uuid4

from memex_core.memory.confidence import confidence_weight
from memex_core.memory.reflect.prompts import ReflectEvidenceContext, ReflectMemoryContext
from memex_core.memory.reflect.utils import build_memory_context
from memex_core.memory.sql_models import MemoryUnit
from memex_common.types import FactTypes


def _make_unit(
    text: str = 'test fact',
    fact_type: str = FactTypes.OPINION,
    confidence_alpha: float | None = None,
    confidence_beta: float | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        id=uuid4(),
        note_id=uuid4(),
        text=text,
        fact_type=fact_type,
        vault_id=uuid4(),
        confidence_alpha=confidence_alpha,
        confidence_beta=confidence_beta,
        occurred_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_build_memory_context_opinion_has_confidence():
    """Opinion units should have confidence populated."""
    unit = _make_unit('opinion text', confidence_alpha=8.0, confidence_beta=2.0)
    contexts = build_memory_context([unit])

    assert len(contexts) == 1
    assert contexts[0].confidence is not None
    assert abs(contexts[0].confidence - 0.8) < 0.01


def test_build_memory_context_fact_has_none_confidence():
    """Non-opinion units should have confidence=None."""
    unit = _make_unit('world fact', fact_type=FactTypes.WORLD)
    contexts = build_memory_context([unit])

    assert len(contexts) == 1
    assert contexts[0].confidence is None


def test_build_memory_context_with_index_map():
    """Index map should override sequential indexing."""
    unit = _make_unit('test')
    index_map = {unit.id: 42}
    contexts = build_memory_context([unit], index_map=index_map)

    assert contexts[0].index_id == 42


def test_confidence_weight_opinions_vs_facts():
    """Opinions get weighted by confidence, facts get factor 1.0."""
    assert confidence_weight(None) == 1.0
    assert confidence_weight(1.0) == 1.0
    assert abs(confidence_weight(0.0) - 0.3) < 0.01
    assert abs(confidence_weight(0.5) - 0.65) < 0.01


def test_confidence_weighted_similarity_scoring():
    """Higher confidence should boost similarity ranking."""
    high_conf_score = 0.7 * confidence_weight(0.9)
    low_conf_score = 0.7 * confidence_weight(0.1)

    assert high_conf_score > low_conf_score


def test_reflect_memory_context_serialization():
    """ReflectMemoryContext should serialize confidence field."""
    ctx = ReflectMemoryContext(index_id=1, content='test', occurred='2026-01-01', confidence=0.85)
    data = ctx.model_dump()
    assert data['confidence'] == 0.85


def test_reflect_evidence_context_serialization():
    """ReflectEvidenceContext should serialize confidence field."""
    ctx = ReflectEvidenceContext(index_id=1, quote='test', occurred='2026-01-01', confidence=0.6)
    data = ctx.model_dump()
    assert data['confidence'] == 0.6
