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
    assert 'multi_select' in body.lower(), '/continue must use an AskUserQuestion multi-select UI'
    assert (
        'one or more' in body.lower() or 'multiple' in body.lower() or 'any number' in body.lower()
    ), '/continue must explicitly allow multiple handoff selections'
    # Only after selection does the agent read the notes and summarize.
    assert 'read' in body.lower() and 'summarize' in body.lower(), (
        '/continue must read the selected handoffs before summarizing'
    )
    # After the summary, the agent asks for next steps rather than assuming.
    assert 'next' in body.lower() and 'ask' in body.lower(), (
        '/continue must ask what the next steps are after summarizing'
    )


def test_continue_fits_askuser_question_option_cap() -> None:
    """AskUserQuestion caps a question at 4 options. The skill must reserve one
    slot for `more` and so present at most 3 real handoffs — never claim to show
    5. Pins the fix for the error+retry round trip the old `limit=5` caused."""
    body = _read_skill('continue')
    assert 'limit=4' in body, '/continue must fetch limit=4 (3 notes + more = 4 options)'
    assert '3 most recent' in body.lower(), '/continue must state it shows the 3 most recent, not 5'
    assert '4 options' in body, '/continue must document the AskUserQuestion 4-option cap'


def test_continue_loads_surgically_in_tiers_not_full_fetch() -> None:
    """The default summary must be built from list_notes output (topic +
    key_points), escalating to a surgical section fetch only when thin. Guards
    against a regression to 'read every selected handoff' unconditionally."""
    body = _read_skill('continue')
    # Tier 0 default: summarize from the list output already in hand.
    assert 'key_points' in body, (
        '/continue must summarize from list_notes key_points by default (Tier 0)'
    )
    assert 'Tier 0' in body and 'Tier 1' in body, '/continue must define the tiered load design'
    # Escalation is on-demand, not unconditional.
    assert 'escalate' in body.lower() or 'on demand' in body.lower(), (
        '/continue must escalate the deep fetch on demand, not always'
    )
    # The old unconditional-full-fetch instruction must be gone.
    assert 'read every selected handoff' not in body.lower(), (
        '/continue must not instruct an unconditional full read of every selected handoff'
    )
    # Tier 1 fetches only load-bearing sections, not all of them.
    assert 'Summary' in body and 'Next steps' in body, (
        '/continue Tier 1 must name the Summary + Next steps sections to fetch'
    )


def test_continue_more_handler_dedups_client_side() -> None:
    """memex_list_notes exposes no offset; the `more` handler must dedup against
    already-shown IDs rather than re-presenting the same first notes."""
    body = _read_skill('continue')
    assert 'dedup' in body.lower(), '/continue `more` handler must dedup client-side'
