"""TC-11-3: Tier A labelled-span markers exist verbatim in shared files.

Pure unit test — read each shared file and assert the expected marker text
is present. Catches regressions where a feature WS accidentally deletes
another WS's labelled block.
"""

from __future__ import annotations

import pathlib as plb

import pytest


_REPO_ROOT = plb.Path(__file__).resolve().parents[4]


_FILE_MARKERS: dict[str, list[str]] = {
    'packages/mcp/src/memex_mcp/server.py': [
        '# Tier A — Tool registry',
        '# --- F4 ---',
        '# --- F5 ---',
        '# --- F8 ---',
        '# --- F9 ---',
        '# --- F20 ---',
        '# --- F32 ---',
    ],
    'packages/core/src/memex_core/scheduler.py': [
        '# Tier A — Scheduler tasks (under MEMEX_LEADER_LOCK_ID)',
        '# --- F6 lint ---',
        '# --- F20 revisit ---',
        '# --- F32 diagnostics ---',
        '# --- F38 consolidation ---',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/tools.py': [
        '# Tier A — Hermes sync wrappers',
        '# --- F4 ---',
        '# --- F5 ---',
        '# --- F8 ---',
        '# --- F9 ---',
        '# --- F20 ---',
        '# --- F32 ---',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py': [
        '# Tier A — Briefing blocks',
        '# --- F6 ---',
        '# --- F14 ---',
        '# --- F20 ---',
        '# --- F32 ---',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/templates.py': [
        '# Tier A — Prompt-fragment templates',
        '# --- F4 ---',
        '# --- F5 ---',
        '# --- F8 ---',
        '# --- F9 ---',
        '# --- F20 ---',
        '# --- F32 ---',
    ],
    'packages/claude-code-plugin/skills/recall/SKILL.md': [
        'Tier A — /recall verb extensions',
        '# --- F8 ---',
        '# --- F20 ---',
        '# --- F32 ---',
    ],
    'packages/claude-code-plugin/skills/remember/SKILL.md': [
        'Tier A — /remember verb extensions',
        '# --- F4 ---',
        '# --- F5 ---',
        '# --- F9 ---',
        '# --- F14 ---',
        '# --- F20 ---',
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
            f'{rel_path}: missing labelled-span marker {marker!r} — '
            'Tier A discipline broken; another WS likely deleted this block'
        )
