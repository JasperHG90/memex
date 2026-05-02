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

    Multiple skills may mention `memex_memory_deprioritize` (e.g. /recall
    surfaces it as a low-MW lint auto-resolve action), but the
    deprioritize-vs-archive verb-pair contrast lives on the write-side
    (/remember). Find any skill that contains BOTH the verb AND the
    contrast — do not stop at the first file that just names the verb.
    """
    found_in: Path | None = None
    for p in _candidate_skill_files():
        if not p.exists():
            continue
        text = p.read_text()
        if 'memex_memory_deprioritize' not in text:
            continue
        lower = text.lower()
        has_contrast = ('non-destructive' in lower and 'archive' in lower) or (
            'deprioritize' in lower and 'destructive' in lower
        )
        if has_contrast:
            found_in = p
            break
    assert found_in is not None, (
        'No CC plugin skill mentions memex_memory_deprioritize AND explicitly '
        'contrasts it with archive (Wave 0 §6 #12)'
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
