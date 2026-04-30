"""F32 Claude Code skill — verb description grep (Test 9).

The recall and remember SKILL.md files are the agent-facing entry points for
the Claude Code plugin. F32 is surfaced via /recall (vault diagnostics is a
read action). This test asserts that at least one SKILL.md mentions the verb
``memex_get_diagnostics_summary`` together with at least one of the documented
diagnostic keywords (cluster count, MW score, top entities).
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
    keyword_pat = re.compile(r'cluster.?count|MW.?score|top.?(retrieved.?)?entit', re.IGNORECASE)
    has_context = any(keyword_pat.search(text) for _, text in mentions)
    assert has_context, (
        'memex_get_diagnostics_summary mentioned but no diagnostic keyword '
        '(cluster count / MW score / top entities) appears alongside it.'
    )
