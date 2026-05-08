"""F43 — Claude Code plugin rule + skill content pinning.

The Claude Code plugin has no Python test harness, so this lives in the root
tests dir. Asserts the new rule file (`rules/memory-resolution-flow.md`) and
the updated skill descriptions (`skills/remember/SKILL.md`,
`skills/recall/SKILL.md`) carry the §3.5 5-step flow + §3.4.1 axes table +
§3.4.2 historical-routing rule.

Source: BACKLOG.md F43 step 3 (Claude Code plugin scope).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / 'packages' / 'claude-code-plugin'
_RULE_FILE = _PLUGIN_ROOT / 'rules' / 'memory-resolution-flow.md'
_REMEMBER_SKILL = _PLUGIN_ROOT / 'skills' / 'remember' / 'SKILL.md'
_RECALL_SKILL = _PLUGIN_ROOT / 'skills' / 'recall' / 'SKILL.md'


def _read(path: Path) -> str:
    assert path.exists(), f'F43 expects {path} to exist (per BACKLOG step 3)'
    return path.read_text()


@pytest.mark.parametrize(
    'kw',
    [
        'Disambiguate',
        'Route by info quality',
        'A: entity-anchored',
        'B: cross-note semantic',
        'C: single-note PageIndex',
        '`top_k`',
        '≥30',
        'LLM-judge',
        'memex_record_outcome',
        'memex_memory_deprioritize',
        'exploration is the safety net',
        'Imperfect recall',
        'memex_get_unit_history',
        'apply_pre_filter=False',
        'evolved',
        'used to',
        'audit',
        'memex_resolve',
        'resolved_at',
        'resolution_type',
        'bulk-by-source',
        'Note-level deprioritize',
    ],
)
def test_resolution_flow_rule_carries_keyword(kw: str) -> None:
    """The new rule file teaches each canonical §3.5 / §3.4.1 / §3.4.2 keyword."""
    text = _read(_RULE_FILE)
    assert kw in text, (
        f'Claude Code plugin rule file is missing keyword {kw!r}. '
        'See cognitive-memory-research-report.md §3.5 / §3.4.1 / §3.4.2.'
    )


def test_remember_skill_references_resolution_flow() -> None:
    """The /remember skill description points at the resolution flow."""
    text = _read(_REMEMBER_SKILL)
    assert 'user reports issue fixed' in text
    assert 'memex_record_outcome' in text
    assert 'memex_memory_deprioritize' in text
    # The skill uses short-form `(A)/(B)/(C)` references back to the rule file.
    assert '(A)' in text
    assert '(B)' in text
    assert '(C)' in text
    assert 'top_k' in text


def test_recall_skill_references_historical_routing_rule() -> None:
    """The /recall skill description teaches the historical-routing rule."""
    text = _read(_RECALL_SKILL)
    assert 'Historical' in text or 'historical' in text
    assert 'memex_get_unit_history' in text
    assert 'apply_pre_filter=False' in text
    assert 'evolved' in text


def test_session_start_hook_copies_all_rule_files() -> None:
    """The on_session_start.sh hook auto-installs every *.md in rules/ (not just memex.md)."""
    hook = _PLUGIN_ROOT / 'scripts' / 'on_session_start.sh'
    text = _read(hook)
    # Hook should iterate the rules directory rather than hard-coding memex.md only.
    assert '/rules' in text, 'session-start hook should reference the rules dir'
    assert 'for ' in text, (
        'on_session_start.sh must iterate all rule files in rules/, not hard-code '
        'memex.md only (otherwise memory-resolution-flow.md never lands in the '
        'project .claude/rules/).'
    )
    # Negative: must not be hard-coded to a single rule file.
    assert text.count('rules/memex.md') <= 1, (
        'on_session_start.sh appears to hard-code memex.md instead of iterating *.md.'
    )
