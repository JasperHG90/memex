import dspy


class TriageNewUnits(dspy.Signature):
    """Identify memory units that explicitly correct, update, revise, or supersede prior information.
    Most units are genuinely new — only flag units with clear corrective language.
    Be conservative: only flag units that sound like corrections or updates, not new information."""

    units: str = dspy.InputField(
        description='JSON list of {id, text} for all new units from the document'
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
    candidates: str = dspy.InputField(
        description='JSON list of {id, text, date} for existing candidate units'
    )
    relationships: list[dict] = dspy.OutputField(
        description=(
            'List of {existing_id: str, relation: "reinforce"|"weaken"|"contradict",'
            ' authoritative: "new"|"existing", reasoning: str}. Only non-neutral pairs.'
        )
    )
