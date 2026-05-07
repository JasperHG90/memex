"""Tier A labelled-span anchors must remain in shared files.

The Hermes plugin scaffolding and MCP tool registry rely on per-file
section headers (``# Tier A — ...``) as structural anchors when shipping
WS output into the right slot. Test that those anchors still exist.

The original guard enforced exact ticket-ref marker text (``# --- F4 ---``
etc.). That text has been intentionally stripped from agent-facing skill
files (per the project's no-ticket-refs policy) but the *file-level*
section headers in source remain as code-review checkpoints. This test
encodes only the survivor set.
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
        # WS slot anchors (Hermes scaffolding writes generated code into
        # these blocks). If any disappear, the WS pipeline silently
        # produces no-op stubs.
        '# --- F4 ---',
        '# --- F5 ---',
        '# --- F8 ---',
        '# --- F9 ---',
        '# --- F20 ---',
        '# --- F32',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py': [
        'Tier A — Briefing blocks',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/templates.py': [
        'Tier A — Prompt-fragment templates',
        '# --- F4 ---',
        '# --- F5 ---',
        '# --- F8 ---',
        '# --- F9 ---',
        '# --- F20 ---',
        '# --- F32',
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
