"""Regression tests for virtual-unit MCP serialization (GH #47: Ghost IDs).

Virtual MemoryUnits surfaced from MentalModel observations flow through
``_build_memory_unit_model``; the McpObservation it returns must expose
``virtual=True`` + ``mental_model_id`` + ``evidence_ids`` so MCP clients can
avoid point-lookups on synthetic ids.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from memex_mcp.models import Staleness
from memex_mcp.server import _build_memory_unit_model


def _dto(*, virtual: bool, mm_id=None, evidence_ids=None):
    metadata: dict = {}
    if virtual:
        metadata = {
            'virtual': True,
            'observation': True,
            'mental_model_id': str(mm_id),
            'evidence_ids': [str(e) for e in (evidence_ids or [])],
            'trend': 'new',
        }
    return SimpleNamespace(
        id=uuid4(),
        text='[Entity] Obs: body',
        fact_type='observation',
        score=0.9,
        confidence=1.0,
        note_id=None if virtual else uuid4(),
        node_ids=[],
        status='active',
        metadata=metadata,
        superseded_by=[],
        event_date=None,
        mentioned_at=None,
        occurred_start=None,
        occurred_end=None,
    )


def test_build_memory_unit_model_flags_virtual():
    mm_id = uuid4()
    evidence_ids = [uuid4(), uuid4()]
    model = _build_memory_unit_model(_dto(virtual=True, mm_id=mm_id, evidence_ids=evidence_ids))

    assert model.virtual is True
    assert model.mental_model_id == mm_id
    assert model.evidence_ids == evidence_ids
    assert model.note_id is None


def test_build_memory_unit_model_defaults_for_real_units():
    model = _build_memory_unit_model(_dto(virtual=False))

    assert model.virtual is False
    assert model.mental_model_id is None
    assert model.evidence_ids == []
    assert model.note_id is not None


def test_build_memory_unit_model_surfaces_created_at_and_event_date():
    created = datetime(2026, 6, 30, 18, 5, tzinfo=timezone.utc)
    event = datetime(2026, 5, 24, tzinfo=timezone.utc)
    dto = _dto(virtual=False)
    dto.created_at = created
    dto.event_date = event

    model = _build_memory_unit_model(dto)

    assert model.created_at == created
    assert model.event_date == event


def test_build_memory_unit_model_timestamps_default_none():
    """DTOs without the new fields (e.g. old namespaces) stay None, not error."""
    model = _build_memory_unit_model(_dto(virtual=False))
    assert model.created_at is None
    assert model.event_date is None


def test_event_date_does_not_drive_staleness_when_mentioned_at_set():
    """Surfacing event_date must not change staleness anchoring: mentioned_at
    still wins the fallback chain. An ancient event_date with a recent
    mentioned_at must read FRESH, not STALE."""
    now = datetime.now(timezone.utc)
    dto = _dto(virtual=False)
    dto.mentioned_at = now - timedelta(days=2)
    dto.event_date = now - timedelta(days=400)

    model = _build_memory_unit_model(dto)

    # Driven by the 2-day-old mentioned_at (confidence 1.0), not the 400-day
    # event_date that is now also surfaced on the DTO.
    assert model.staleness == Staleness.FRESH
    assert model.event_date == dto.event_date
