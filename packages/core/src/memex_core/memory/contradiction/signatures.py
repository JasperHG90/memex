"""DSPy signatures for contradiction detection."""

from __future__ import annotations

from typing import Literal

import dspy
from pydantic import BaseModel, Field


# ─── Pydantic models ───


class TriageUnit(BaseModel):
    """A memory unit submitted for triage."""

    id: str = Field(description='UUID of the memory unit.')
    text: str = Field(description='Text content of the memory unit.')


class CandidateUnit(BaseModel):
    """An existing memory unit that may be related to a new unit."""

    id: str = Field(description='UUID of the existing memory unit.')
    text: str = Field(description='Text content of the existing unit.')
    date: str = Field(description='ISO-format date of the existing unit, or "unknown".')


class ContradictionRelationship(BaseModel):
    """Classified relationship between a new unit and an existing candidate."""

    existing_id: str = Field(description='UUID of the existing candidate unit.')
    relation: Literal['reinforce', 'weaken', 'contradict'] = Field(
        description='Relationship type: reinforce, weaken, or contradict.'
    )
    authoritative: Literal['new', 'existing'] = Field(
        default='new',
        description='Which unit is authoritative (wins). Defaults to "new" (later date wins).',
    )
    reasoning: str = Field(description='Brief explanation of why this relationship was assigned.')


# ─── DSPy signatures ───


class TriageNewUnits(dspy.Signature):
    """Identify memory units that correct, update, revise, or supersede prior information.
    Flag units where a previously stated fact may no longer hold — including both explicit
    corrections and natural state changes (e.g. someone replaced someone, a value changed).
    Most units are genuinely new — do not flag units that only add new information."""

    units: list[TriageUnit] = dspy.InputField(
        description='List of new units from the document, each with id and text.'
    )
    flagged_ids: list[str] = dspy.OutputField(
        description=(
            'List of unit IDs that contain corrections, updates, or revisions. Empty list if none.'
        )
    )


class ClassifyRelationships(dspy.Signature):
    """Classify the relationship between a new memory unit and existing candidate units.
    For each candidate, determine if the new unit reinforces, weakens, contradicts, or is
    neutral to it.
    Use temporal context: by default, the unit with the later date is authoritative, unless
    content explicitly indicates otherwise.
    ONLY output NON-NEUTRAL relationships. Skip neutral pairs entirely to save tokens."""

    new_unit_text: str = dspy.InputField(description='Text of the new memory unit')
    new_unit_date: str = dspy.InputField(description='Date of the new unit (ISO format)')
    candidates: list[CandidateUnit] = dspy.InputField(
        description='Existing candidate units with id, text, and date.'
    )
    relationships: list[ContradictionRelationship] = dspy.OutputField(
        description='List of non-neutral relationships. Only reinforce, weaken, or contradict.'
    )
