"""F4 — Wave 0 invariant grep guards (AC-X-4).

Static check: F4 source files contain NO Wave 0 anti-patterns:
- Zero `cascade_to_models` flags (archive cascading is the destructive verb;
  deprioritize must NEVER add a cascade flag).
- Zero `access_count` references (per AC-X-4: access_count must not return).
"""

from __future__ import annotations

from pathlib import Path


_F4_SOURCE_FILES: tuple[Path, ...] = tuple(
    Path(__file__).resolve().parent.parent / p
    for p in (
        'packages/core/src/memex_core/services/units.py',
        'packages/core/src/memex_core/server/memories.py',
        'packages/mcp/src/memex_mcp/_f4_descriptions.py',
    )
)


def test_no_cascade_to_models_flag_in_f4_sources():
    for f in _F4_SOURCE_FILES:
        text = f.read_text()
        assert 'cascade_to_models' not in text, (
            f'{f.name}: Wave 0 §6 #12 invariant breached — '
            'F4 must not introduce cascade_to_models flag'
        )


def test_no_access_count_references_in_f4_sources():
    for f in _F4_SOURCE_FILES:
        text = f.read_text()
        assert 'access_count' not in text, (
            f'{f.name}: AC-X-4 invariant breached — access_count must not return'
        )
