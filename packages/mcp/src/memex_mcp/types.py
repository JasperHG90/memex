import enum


class NoteTemplateType(str, enum.Enum):
    TECHNICAL_BRIEF = 'technical_brief'
    GENERAL_NOTE = 'general_note'
    ADR = 'architectural_decision_record'
    RFC = 'request_for_comments'
    QUICK_NOTE = 'quick_note'
