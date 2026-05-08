"""Claude Code skill — verb description grep (diagnostics).

The recall and remember SKILL.md files are the agent-facing entry points for
the Claude Code plugin. Diagnostics is surfaced via /recall. This test asserts
that at least one SKILL.md mentions the verb ``memex_get_diagnostics_summary``
together with at least one of the documented diagnostic keywords (cluster count,
Memory Worth score, top entities).

The negative-grep test below codifies the architectural rule that
diagnostics is a *read* action (recall), not a capture action (remember).
"""

from __future__ import annotations

import re
from pathlib import Path


_PACKAGES_DIR = Path(__file__).resolve().parents[2]
SKILL_FILES = [
    _PACKAGES_DIR / 'claude-code-plugin' / 'skills' / 'recall' / 'SKILL.md',
    _PACKAGES_DIR / 'claude-code-plugin' / 'skills' / 'remember' / 'SKILL.md',
]


def test_rule_text_describes_verb():
    mentions = []
    for path in SKILL_FILES:
        assert path.exists(), f'SKILL.md not found at {path}'
        text = path.read_text()
        if 'memex_get_diagnostics_summary' in text:
            mentions.append((path, text))

    assert mentions, (
        'Neither recall/SKILL.md nor remember/SKILL.md mentions '
        'memex_get_diagnostics_summary. F32 verb must be surfaced.'
    )

    # At least one mention must include a documented keyword.
    keyword_pat = re.compile(
        r'cluster.?count|Memory.?Worth.?score|MW.?score|top.?(retrieved.?)?entit', re.IGNORECASE
    )
    has_context = any(keyword_pat.search(text) for _, text in mentions)
    assert has_context, (
        'memex_get_diagnostics_summary mentioned but no diagnostic keyword '
        '(cluster count / Memory Worth score / top entities) appears alongside it.'
    )


def test_remember_skill_does_not_contain_diagnostics_verb():
    """Diagnostics is a read action; belongs in recall/SKILL.md, NOT remember/SKILL.md.

    Codifies the architectural call so future refactors can't silently un-make it.
    """
    skill_path = _PACKAGES_DIR / 'claude-code-plugin' / 'skills' / 'remember' / 'SKILL.md'
    assert skill_path.exists(), f'SKILL.md not found at {skill_path}'
    text = skill_path.read_text()
    assert 'memex_get_diagnostics_summary' not in text, (
        'Diagnostics verb leaked into remember/SKILL.md — it is a read action, '
        'belongs in recall only. Move it back.'
    )
