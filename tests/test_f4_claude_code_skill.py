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
    found_in: Path | None = None
    for p in _candidate_skill_files():
        if p.exists() and 'memex_memory_deprioritize' in p.read_text():
            found_in = p
            break
    assert found_in is not None, (
        'memex_memory_deprioritize verb not described in any CC plugin skill'
    )

    text = found_in.read_text()
    assert 'memex_memory_deprioritize' in text
    assert ('archive' in text.lower()) or ('destructive' in text.lower())

    has_contrast = ('non-destructive' in text.lower() and 'archive' in text.lower()) or (
        'deprioritize' in text.lower() and 'destructive' in text.lower()
    )
    assert has_contrast, (
        'CC skill must explicitly contrast deprioritize (non-destructive) vs archive '
        '(destructive) per Wave 0 §6 #12'
    )
