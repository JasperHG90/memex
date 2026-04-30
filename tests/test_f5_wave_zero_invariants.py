"""F5 — Wave 0 invariant grep guards (AC-X-4).

Static check: F5 source files contain NO Wave 0 anti-patterns. The
synchronous summarize-node verb is non-destructive — it must not invoke
``set_status(ARCHIVED)``, ``delete()``, or any cascade flag.
"""

from __future__ import annotations

import re
from pathlib import Path


_F5_SOURCE_FILES: tuple[Path, ...] = tuple(
    Path(__file__).resolve().parent.parent / p
    for p in (
        'packages/core/src/memex_core/services/reflection.py',
        'packages/core/src/memex_core/server/reflection.py',
        'packages/core/src/memex_core/services/rate_limit.py',
        'packages/mcp/src/memex_mcp/_f5_descriptions.py',
    )
)


def test_no_destructive_calls_in_f5_sources():
    """F5 must never delete or archive memory units."""
    forbidden = (
        re.compile(r'\bArchive\b'),
        re.compile(r'\.delete\b'),
        re.compile(r'set_status\s*\(\s*ContentStatus\.ARCHIVED'),
    )
    for f in _F5_SOURCE_FILES:
        text = f.read_text()
        for pattern in forbidden:
            assert not pattern.search(text), (
                f'{f.name}: Wave 0 §6 #12 invariant breached — F5 must not '
                f'invoke destructive verb (matched {pattern.pattern})'
            )


def test_no_cascade_to_models_flag_in_f5_sources():
    for f in _F5_SOURCE_FILES:
        text = f.read_text()
        assert 'cascade_to_models' not in text, (
            f'{f.name}: Wave 0 §6 #12 invariant breached — '
            'F5 must not introduce cascade_to_models flag'
        )


def test_no_access_count_references_in_f5_sources():
    for f in _F5_SOURCE_FILES:
        text = f.read_text()
        assert 'access_count' not in text, (
            f'{f.name}: AC-X-4 invariant breached — access_count must not return'
        )
