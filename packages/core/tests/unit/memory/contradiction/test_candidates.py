"""Unit tests for contradiction candidate retrieval."""

from datetime import datetime, timezone
from uuid import uuid4, UUID

from memex_core.memory.contradiction.candidates import (
    _source_diverse_select,
)
from memex_core.memory.sql_models import MemoryUnit, ContentStatus


def _make_unit(
    note_id: UUID | None = None,
    text: str | None = None,
    vault_id: UUID | None = None,
    embedding: list[float] | None = None,
) -> MemoryUnit:
    """Create a test MemoryUnit."""
    return MemoryUnit(
        id=uuid4(),
        text=text or f'Test fact {uuid4()}',
        fact_type='world',
        status=ContentStatus.ACTIVE,
        event_date=datetime.now(timezone.utc),
        vault_id=vault_id or uuid4(),
        note_id=note_id or uuid4(),
        embedding=embedding or [0.1] * 384,
        confidence=1.0,
    )


class TestSourceDiverseSelect:
    """Tests for _source_diverse_select."""

    def test_returns_all_when_under_k(self):
        """When fewer candidates than k, return all."""
        candidates = [_make_unit() for _ in range(5)]
        result = _source_diverse_select(candidates, k=10)
        assert len(result) == 5

    def test_caps_at_k(self):
        """When more candidates than k, cap at k."""
        candidates = [_make_unit() for _ in range(20)]
        result = _source_diverse_select(candidates, k=5)
        assert len(result) == 5

    def test_round_robin_across_notes(self):
        """Ensure round-robin picks from different notes."""
        note_a = uuid4()
        note_b = uuid4()
        candidates = [_make_unit(note_id=note_a) for _ in range(10)] + [
            _make_unit(note_id=note_b) for _ in range(2)
        ]
        result = _source_diverse_select(candidates, k=4)
        note_ids = {u.note_id for u in result}
        assert note_a in note_ids
        assert note_b in note_ids

    def test_single_note_dominance_limited(self):
        """One verbose note shouldn't dominate all slots."""
        note_big = uuid4()
        note_small = uuid4()
        candidates = [_make_unit(note_id=note_big) for _ in range(50)] + [
            _make_unit(note_id=note_small),
        ]
        result = _source_diverse_select(candidates, k=4)
        small_count = sum(1 for u in result if u.note_id == note_small)
        assert small_count >= 1

    def test_empty_candidates(self):
        """Empty input returns empty output."""
        result = _source_diverse_select([], k=5)
        assert result == []

    def test_exact_k_returns_all(self):
        """When candidates == k, return all unchanged."""
        candidates = [_make_unit() for _ in range(5)]
        result = _source_diverse_select(candidates, k=5)
        assert len(result) == 5
        assert set(u.id for u in result) == set(u.id for u in candidates)

    def test_three_notes_balanced(self):
        """Three notes with unequal counts get balanced selection."""
        notes = [uuid4() for _ in range(3)]
        candidates = (
            [_make_unit(note_id=notes[0]) for _ in range(10)]
            + [_make_unit(note_id=notes[1]) for _ in range(5)]
            + [_make_unit(note_id=notes[2]) for _ in range(1)]
        )
        result = _source_diverse_select(candidates, k=6)
        note_ids = {u.note_id for u in result}
        assert len(note_ids) == 3
