"""F4 — Claude Code plugin skill text invariant (AC-F4-6, T8).

Mirrors the QA-supplied shape: assert at least one CC plugin SKILL.md
mentions `memex_memory_deprioritize` and explicitly contrasts it with
archive (Wave 0 §6 #12).
"""

from __future__ import annotations

from pathlib import Path


def _candidate_skill_files() -> list[Path]:
    base = Path(__file__).resolve().parent.parent / 'packages' / 'claude-code-plugin' / 'skills'
    return [
        base / 'recall' / 'SKILL.md',
        base / 'remember' / 'SKILL.md',
    ]


def test_deprioritize_skill_describes_non_destructive_verb():
    """At least one CC plugin skill mentions the verb + the non-destructive
    contrast vs archive (Wave 0 §6 #12).
    """
    matching = [
        p for p in _candidate_skill_files()
        if p.exists() and 'memex_memory_deprioritize' in p.read_text()
    ]
    assert matching, 'memex_memory_deprioritize verb not described in any CC plugin skill'

    contrast_satisfied = False
    for p in matching:
        text = p.read_text().lower()
        if ('non-destructive' in text and 'archive' in text) or (
            'deprioritize' in text and 'destructive' in text
        ):
            contrast_satisfied = True
            break

    assert contrast_satisfied, (
        f'At least one of {[str(p) for p in matching]} must contrast deprioritize '
        '(non-destructive) vs archive (destructive) per Wave 0 §6 #12'
    )
