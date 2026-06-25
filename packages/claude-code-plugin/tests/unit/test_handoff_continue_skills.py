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


def test_continue_vault_scopes_list_notes() -> None:
    """The #1 /continue failure: listing handoffs without a vault_id queries the
    server's default read scope, not the project vault where /handoff wrote them —
    so it finds nothing even when the vault has handoffs. The skill must scope to
    the project vault and document the all-vaults wildcard."""
    body = _read_skill('continue')
    assert 'continue_vault_scope' in body, (
        '/continue must carry the load-bearing vault-scope constraint'
    )
    # Lists must pass a vault_id (project vault), not omit it.
    assert 'vault_id="<project vault>"' in body, (
        '/continue list_notes calls must scope to the project vault'
    )
    # The all-vaults broaden must use the documented wildcard, not omission.
    assert 'vault_id="*"' in body, '/continue must document the "*" all-vaults wildcard'


def test_learnings_has_valid_frontmatter() -> None:
    fm = _frontmatter(_read_skill('learnings'))
    assert re.search(r'^name:\s*learnings\s*$', fm, re.MULTILINE), (
        'frontmatter name must be `learnings` and match the directory'
    )
    assert re.search(r'^description:\s*\S', fm, re.MULTILINE), 'description required'
    assert re.search(r'^argument-hint:\s*\S', fm, re.MULTILINE), 'argument-hint required'


def test_learnings_mandates_note_for_insights() -> None:
    """The #1 failure mode: distilling session insights but routing them to a
    handoff / KV / case instead of writing them as notes. The skill must make the
    note the enforced home for an insight — not one of three equal options."""
    body = _read_skill('learnings')

    # The note tool must be present AND framed as mandatory for insights.
    assert 'memex_add_note' in body, '/learnings must write insights as notes'
    assert 'insight_must_be_a_note' in body, (
        '/learnings must carry the load-bearing constraint that an insight is not '
        'captured until it is a memex_add_note'
    )

    # The non-substitution rule must name all three wrong homes explicitly, so an
    # agent cannot rationalize "already captured" via a handoff/KV/case.
    constraint_zone = body[body.index('insight_must_be_a_note') :]
    for wrong_home in ('handoff', 'memex_kv_put', 'memex_case_submit'):
        assert wrong_home in constraint_zone, (
            f'the non-substitution rule must explicitly reject {wrong_home!r} as an '
            "insight's home"
        )

    # A completion gate: zero notes after finding insights == mis-routed.
    assert re.search(r'zero\b[^\n]*memex_add_note', body), (
        '/learnings must include a verify gate flagging zero-notes-after-insights as '
        'a mis-route'
    )
