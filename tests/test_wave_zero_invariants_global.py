"""Cross-cutting Wave 0 invariant guards — runtime package source.

Per Tier-A Wave 0 §6 #12 + AC-X-4: no runtime source file under
packages/*/src may introduce `cascade_to_models` (archive cascading is
the destructive verb; non-destructive curation must NEVER cascade) or
`access_count` (deprecated counter forbidden by AC-X-4).

Alembic migrations under `alembic/versions/` are excluded — they are
append-only schema history and may legitimately reference removed columns
(e.g. migration 023 drops the dead `access_count` column).

Lands in F5's PR to close the deferred-from-F4 cross-cutting gap. Each
new feature inherits this guard automatically — no per-feature grep test
needed for these two terms once this is live.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r'\bcascade_to_models\b', 'Wave 0 §6 #12 invariant: cascade_to_models forbidden'),
    (r'\baccess_count\b', 'AC-X-4 invariant: access_count must not return'),
)


def _src_python_files() -> list[Path]:
    excluded_dirs = {'tests', 'versions'}
    return [
        p for p in _REPO_ROOT.glob('packages/*/src/**/*.py') if not (excluded_dirs & set(p.parts))
    ]


def test_no_forbidden_terms_in_package_source():
    files = _src_python_files()
    assert files, 'No package source files found — glob is wrong'
    for pattern, message in _FORBIDDEN_PATTERNS:
        regex = re.compile(pattern)
        offenders = [str(p.relative_to(_REPO_ROOT)) for p in files if regex.search(p.read_text())]
        assert not offenders, f'{message}\nOffending files:\n  ' + '\n  '.join(offenders)
