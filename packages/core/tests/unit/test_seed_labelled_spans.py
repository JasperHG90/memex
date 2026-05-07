"""Tier A labelled-span anchors must remain in shared files.

The MCP tool registry, scheduler, and Hermes plugin templates rely on
per-file section headers (``# Tier A — ...``) as structural anchors that
code review keys off. This test asserts those headers exist.

NOTE: The previous version of this guard also enforced ticket-ref markers
like ``# --- F4 ---`` in tools.py and templates.py. Those tickets are a
project policy violation per ``feedback_no_ticket_refs_in_user_facing_surfaces``
and should be stripped from source in a future cleanup pass — this test
deliberately does NOT mandate them so the cleanup is not blocked.
"""

from __future__ import annotations

import pathlib as plb

import pytest


_REPO_ROOT = plb.Path(__file__).resolve().parents[4]


_FILE_MARKERS: dict[str, list[str]] = {
    'packages/mcp/src/memex_mcp/server.py': ['Tier A — Tool registry'],
    'packages/core/src/memex_core/scheduler.py': [
        'Tier A — Scheduler tasks (under MEMEX_LEADER_LOCK_ID)',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/tools.py': [
        'Tier A — Hermes sync wrappers',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py': [
        'Tier A — Briefing blocks',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/templates.py': [
        'Tier A — Prompt-fragment templates',
    ],
}


@pytest.mark.parametrize(
    'rel_path,markers',
    list(_FILE_MARKERS.items()),
    ids=list(_FILE_MARKERS.keys()),
)
def test_marker_text_present(rel_path: str, markers: list[str]) -> None:
    src = (_REPO_ROOT / rel_path).read_text(encoding='utf-8')
    for marker in markers:
        assert marker in src, (
            f'{rel_path}: missing labelled-span anchor {marker!r} — '
            'a downstream WS likely deleted this section header'
        )
