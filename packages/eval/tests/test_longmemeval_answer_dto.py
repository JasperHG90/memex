"""Tests for the LongMemEval answer module's shape (M5 + M6).

The answer module no longer renders prompts in-process — it shells
out to a Claude Code subagent (M6). What remains DTO-contract-worthy
on the Python side is the ``MemoryUnitDTO`` import surface (the
subagent uses the MCP tools which return DTOs through the client).
These tests assert:

- ``MemoryUnitDTO`` exposes the fields the subagent's retrieval
  playbook relies on: ``text``, ``fact_type``, ``occurred_start``,
  ``mentioned_at``. If any are renamed upstream, the test fails loudly
  instead of the subagent silently seeing empty strings.
- ``FactTypes`` serialises via ``.value`` so the MCP surface carries
  the string "event"/"world"/"observation" rather than ``FactTypes.EVENT``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from memex_common.schemas import MemoryUnitDTO
from memex_common.types import FactTypes


def test_memory_unit_dto_exposes_expected_fields() -> None:
    dt = datetime(2024, 5, 1, 9, 30, tzinfo=timezone.utc)
    unit = MemoryUnitDTO(
        id=uuid4(),
        text='Project kickoff.',
        fact_type=FactTypes.EVENT,
        occurred_start=dt,
        mentioned_at=dt,
    )
    # Names the MCP path and the subagent's retrieval playbook rely on.
    assert unit.text == 'Project kickoff.'
    assert unit.fact_type is FactTypes.EVENT
    assert unit.occurred_start == dt
    assert unit.mentioned_at == dt


def test_fact_type_value_is_string_not_enum_repr() -> None:
    """The MCP surface and any downstream JSON serialiser must render
    ``FactTypes`` via its ``.value`` so clients see ``"event"``, not
    ``"FactTypes.EVENT"``."""
    assert FactTypes.EVENT.value == 'event'
    assert FactTypes.WORLD.value == 'world'
    assert FactTypes.OBSERVATION.value == 'observation'


def test_memory_unit_dto_does_not_have_event_date_attribute() -> None:
    """Pre-M5 code used ``getattr(u, 'event_date', None)``. This test
    pins the reality: ``event_date`` is NOT on the DTO — operators
    must go through ``occurred_start`` / ``mentioned_at``."""
    unit = MemoryUnitDTO(
        id=uuid4(),
        text='x',
        fact_type=FactTypes.WORLD,
    )
    assert not hasattr(unit, 'event_date')
