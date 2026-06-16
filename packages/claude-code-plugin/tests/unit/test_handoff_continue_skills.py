"""Content invariants for the /handoff and /continue plugin skills.

These are path-based assertions on the SKILL.md files — no DB, no hook
execution. They pin the load-bearing routing each skill must keep:

- valid frontmatter (name / description / argument-hint), name matching dir;
- /handoff writes a `handoff`-tagged note and does NOT file a case (the
  one-write-one-plane discipline — a handoff is a work summary, not a
  reusable procedure);
- /continue retrieves those notes by the `handoff` tag via list-by-recency,
  enumerates candidates, asks the user to select (one, many, or more),
  loads only the selected ones, summarizes them, and then asks for next
  steps.

If the skills are rewritten, these guard against silently dropping the
routing that couples the two skills together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / 'skills'


def _read_skill(name: str) -> str:
    path = SKILLS_DIR / name / 'SKILL.md'
    assert path.exists(), f'{path} does not exist'
    return path.read_text()


def _frontmatter(text: str) -> str:
    """Return the YAML frontmatter block (between the first two `---` fences)."""
    m = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
    assert m, 'SKILL.md must open with a `---` frontmatter block'
    return m.group(1)


@pytest.mark.parametrize('name', ['handoff', 'continue'])
def test_skill_has_valid_frontmatter(name: str) -> None:
    fm = _frontmatter(_read_skill(name))
    assert re.search(rf'^name:\s*{name}\s*$', fm, re.MULTILINE), (
        f'frontmatter name must be `{name}` and match the directory'
    )
    assert re.search(r'^description:\s*\S', fm, re.MULTILINE), 'description required'
    assert re.search(r'^argument-hint:\s*\S', fm, re.MULTILINE), 'argument-hint required'


def test_handoff_writes_tagged_note_not_a_case() -> None:
    body = _read_skill('handoff')
    assert 'memex_add_note' in body, '/handoff must write the handoff as a note'
    assert '["handoff"]' in body, '/handoff must tag the note with the `handoff` tag'
    # A handoff is a work summary, not a reusable how-to — it must not also
    # file a case, or the procedural plane fills with non-procedures.
    assert 'memex_case_submit' not in body or 'do NOT' in body or 'NOT also' in body, (
        '/handoff must not instruct an unconditional memex_case_submit'
    )


def test_continue_is_browse_then_load_not_auto_brief() -> None:
    body = _read_skill('continue')
    assert 'memex_list_notes' in body, '/continue must list handoffs by recency'
    assert 'handoff' in body, '/continue must filter on the `handoff` tag'
    # Candidate list must carry enough information to choose.
    assert 'description' in body and ('date' in body or 'timestamp' in body), (
        '/continue must surface candidates with description + date'
    )
    # User must choose; the agent does not auto-load the latest handoff.
    assert 'which' in body.lower() or 'pick' in body.lower() or 'select' in body.lower(), (
        '/continue must ask the user which handoffs are relevant'
    )
    # The user can pick multiple handoffs.
    assert 'more' in body.lower() or 'numbers' in body.lower() or 'all' in body.lower(), (
        '/continue must allow multiple selections or loading more'
    )
    # Only after selection does the agent read the notes and summarize.
    assert 'read' in body.lower() and 'summarize' in body.lower(), (
        '/continue must read the selected handoffs before summarizing'
    )
    # After the summary, the agent asks for next steps rather than assuming.
    assert 'next' in body.lower() and 'ask' in body.lower(), (
        '/continue must ask what the next steps are after summarizing'
    )
