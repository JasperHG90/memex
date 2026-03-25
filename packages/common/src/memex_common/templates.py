"""Note creation templates shared by CLI and MCP."""

import enum
import pathlib

_prompts_dir = pathlib.Path(__file__).parent / 'prompts'


class NoteTemplateType(str, enum.Enum):
    TECHNICAL_BRIEF = 'technical_brief'
    GENERAL_NOTE = 'general_note'
    ADR = 'architectural_decision_record'
    RFC = 'request_for_comments'
    QUICK_NOTE = 'quick_note'


_TEMPLATE_FILES: dict[NoteTemplateType, str] = {
    NoteTemplateType.TECHNICAL_BRIEF: 'technical_brief_template.md',
    NoteTemplateType.GENERAL_NOTE: 'general_note_template.md',
    NoteTemplateType.ADR: 'adr_template.md',
    NoteTemplateType.RFC: 'rfc_template.md',
}

_QUICK_NOTE_TEMPLATE = '# Note: [Insert title here]\n\n## Content\n[Content in markdown format]'


def get_template(template_type: NoteTemplateType) -> str:
    """Return the markdown template for the given type."""
    if template_type == NoteTemplateType.QUICK_NOTE:
        return _QUICK_NOTE_TEMPLATE

    filename = _TEMPLATE_FILES.get(template_type)
    if filename is None:
        raise ValueError(f'Unknown template type: {template_type}')

    path = _prompts_dir / filename
    return path.read_text()


def list_template_types() -> list[str]:
    """Return the available template type values."""
    return [t.value for t in NoteTemplateType]
