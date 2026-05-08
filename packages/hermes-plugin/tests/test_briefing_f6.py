"""F6 briefing tests (AC-F6-7): pending lint findings block + CLI pointer.

The Hermes briefing surface must:

* Render a "Maintenance findings" section when ``lint_pending_count`` is
  passed and > 0.
* Include the literal CLI pointer ``run `memex lint findings``` so the
  agent (or a human reader) can self-discover the inspection path.
* Suppress the section when the count is 0 or None — no "0 pending"
  noise on a clean vault.
"""

from __future__ import annotations

from memex_hermes_plugin.memex.briefing import (
    _render_lint_block,
    format_briefing_block,
)


def test_render_lint_block_includes_pending_count_and_cli_pointer():
    """Renderer surfaces the integer count + literal CLI pointer."""
    rendered = _render_lint_block(7)
    assert '### Maintenance findings' in rendered
    assert '7 pending lint findings' in rendered
    # AC-F6-7: the literal CLI pointer must be present so the agent has a
    # concrete next action. The compressed phrasing is "Inspect with
    # `memex lint findings`" — accept either verb so the test stays green
    # if the surface is later expanded back to "run `memex lint findings`".
    assert (
        'Inspect with `memex lint findings`' in rendered or 'run `memex lint findings`' in rendered
    )


def test_format_briefing_block_includes_lint_section_when_count_positive():
    """When ``lint_pending_count > 0``, the lint section appears in the briefing."""
    block = format_briefing_block(
        briefing='',
        vault_id='my-vault',
        project_id='proj',
        session_note_key='session/proj/2026-04-30',
        kv_instructions_if_no_vault=False,
        lint_pending_count=3,
    )
    assert '### Maintenance findings' in block
    assert '3 pending lint findings' in block
    assert 'Inspect with `memex lint findings`' in block or 'run `memex lint findings`' in block


def test_format_briefing_block_suppresses_lint_section_when_zero():
    """A clean vault (count=0) must NOT add a "0 pending" line."""
    block = format_briefing_block(
        briefing='',
        vault_id='my-vault',
        project_id='proj',
        session_note_key='session/proj/2026-04-30',
        kv_instructions_if_no_vault=False,
        lint_pending_count=0,
    )
    assert '### Maintenance findings' not in block
    assert 'pending lint findings' not in block


def test_format_briefing_block_suppresses_lint_section_when_none():
    """When the fetch fails (None), the lint section is omitted."""
    block = format_briefing_block(
        briefing='',
        vault_id='my-vault',
        project_id='proj',
        session_note_key='session/proj/2026-04-30',
        kv_instructions_if_no_vault=False,
        lint_pending_count=None,
    )
    assert '### Maintenance findings' not in block
