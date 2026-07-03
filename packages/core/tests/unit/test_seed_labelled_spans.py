"""TC-11-3: Tier A labelled-span markers exist verbatim in shared files.

Pure unit test — read each shared file and assert the expected marker text
is present. Catches regressions where a feature WS accidentally deletes
another WS's labelled block.
"""

from __future__ import annotations

import pathlib as plb

import pytest


_REPO_ROOT = plb.Path(__file__).resolve().parents[4]


# NOTE: F20 was the FSRS-5 revisit slot; it is removed (no longer a Tier A
# feature). All F20 markers have been deleted from the source files. The
# remember/SKILL.md and recall/SKILL.md entries were also rewritten by
# earlier "compress agent surfaces" refactors which dropped the labelled
# headers — they now hold only behavioural content, not Tier A scaffolding.
#
# mcp/server.py is a user/agent-facing surface; #156 (and the later
# "strip ticket/RFC/Hermes refs" refactor) deliberately replaced its F-code
# span markers with semantic section anchors so no ticket IDs leak into the
# MCP surface. The internal hermes-plugin scaffolding (tools.py / templates.py)
# keeps the F-code markers. The anchors below label the SAME Tier A spans the
# F-codes used to: Deprioritize/Restore=F4, Summarize=F5, Lint=F8,
# Consolidation=F9, Diagnostics=F32.
_FILE_MARKERS: dict[str, list[str]] = {
    'packages/mcp/src/memex_mcp/server.py': [
        '# Tier A — Tool registry',
        '# --- Deprioritize / Restore ---',
        '# --- Summarize ---',
        '# --- Lint ---',
        '# --- Consolidation ---',
        '# --- Diagnostics ---',
    ],
    'packages/core/src/memex_core/scheduler.py': [
        '# Tier A — Scheduler tasks (under MEMEX_LEADER_LOCK_ID)',
        '# --- Lint ---',
        '# --- Diagnostics ---',
        '# --- Consolidation ---',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/tools.py': [
        '# Tier A — Hermes sync wrappers',
        '# --- F4 ---',
        '# --- F5 ---',
        '# --- F8 ---',
        '# --- F9 ---',
        '# --- F32 ---',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/briefing.py': [
        '# Tier A — Briefing blocks',
    ],
    'packages/hermes-plugin/src/memex_hermes_plugin/memex/templates.py': [
        '# Tier A — Prompt-fragment templates',
        '# --- F4 ---',
        '# --- F5 ---',
        '# --- F8 ---',
        '# --- F9 ---',
        '# --- F32 ---',
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
