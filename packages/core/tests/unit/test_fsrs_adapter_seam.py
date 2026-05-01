"""TC-24-13: FSRS-5 adapter is the EXACTLY-ONCE seam in memex_core.

Path A directive (post `paper-cross-check.md` verification, 2026-05-01):
`py-fsrs==4.1.2` implements FSRS-5, the current production-grade FSRS
algorithm. F20 ships a thin adapter at `memex_core.memory.revisit` that
delegates to `fsrs.Scheduler`; we do NOT carry a vendored copy of the
algorithm in production.

This drift guard catches two failure modes:
  (a) Lib silently deleted / re-vendored — drops the count to 0.
  (b) A future refactor sneaks `from fsrs import Card` into a different
      module (e.g. `services/some_other_module.py`), bypassing the
      adapter. The count stays >= 1, but the location assertion fires.

Either failure mode produces an actionable message pointing at
`paper-cross-check.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

import memex_core

PKG_ROOT = Path(memex_core.__file__).parent
EXCLUDE_DIRS = ('tests', '__pycache__', 'alembic')

FSRS_IMPORT_PATTERN = re.compile(
    r'^(?:from\s+fsrs(?:\.\S+)?\s+import|import\s+fsrs(?:\s|$))',
    re.MULTILINE,
)

EXPECTED_IMPORTER = Path('memory/revisit.py')


def test_fsrs_imported_exactly_once_in_revisit_module() -> None:
    """FSRS-5 adapter invariant: py-fsrs is imported EXACTLY ONCE in
    `memex_core/`, and that import lives in `memory/revisit.py`.

    Any other import path means a future refactor bypassed the adapter
    seam — flag it loudly and refer the reviewer to
    `.dev-team-artifacts/dev-tier-a-cognitive-memory/pocs/
    003-f20-fsrs-parity/paper-cross-check.md`.
    """
    matches: list[Path] = []
    for py in PKG_ROOT.rglob('*.py'):
        if any(part in EXCLUDE_DIRS for part in py.relative_to(PKG_ROOT).parts):
            continue
        if FSRS_IMPORT_PATTERN.search(py.read_text()):
            matches.append(py.relative_to(PKG_ROOT))

    assert len(matches) == 1, (
        f'Expected exactly one fsrs import in memex_core/; got {len(matches)}: {matches}.\n'
        'See `.dev-team-artifacts/dev-tier-a-cognitive-memory/pocs/'
        '003-f20-fsrs-parity/paper-cross-check.md` for the Path A directive.'
    )
    assert matches[0] == EXPECTED_IMPORTER, (
        f'fsrs import must live in {EXPECTED_IMPORTER}; found in {matches[0]}.\n'
        'A future refactor bypassed the FSRS-5 adapter seam — re-route '
        'all FSRS access through `memex_core.memory.revisit`.'
    )
