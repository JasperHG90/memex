"""TC-23-7 — F9 single-leader exemption invariant (AC-X-4 / AC-X-8).

Static-grep guard: only TWO call sites in `packages/core/src/memex_core/`
may invoke `pg_try_advisory_lock`:
- `scheduler.py` (LEADER lock)
- `services/locks.py` (per-entity lock — F9)

Migration's `pg_advisory_lock` (alembic/env.py) is a different SQL function
and uses a separate MIGRATION_LOCK_ID space — explicitly excluded.

If a third pg_try_advisory_lock call appears in core, this test fails and
forces a design review (AC-X-4: leader exemption invariant).
"""

from __future__ import annotations

import pathlib
import re

import pytest

CORE_SRC_ROOT = pathlib.Path(__file__).resolve().parents[3] / 'src' / 'memex_core'

EXPECTED_PG_TRY_ADVISORY_LOCK_FILES = {
    'scheduler.py',  # LEADER lock
    'services/locks.py',  # F9 per-entity lock
}


def _scan_for_pg_try_advisory_lock() -> dict[str, int]:
    """Walk core source and count `pg_try_advisory_lock` occurrences per file.

    Excludes alembic/env.py (different concern, uses pg_advisory_lock not
    pg_try_advisory_lock) and __pycache__ entries.
    """
    counts: dict[str, int] = {}
    pattern = re.compile(r'pg_try_advisory_lock')
    assert CORE_SRC_ROOT.exists(), f'core src root missing: {CORE_SRC_ROOT}'
    for path in CORE_SRC_ROOT.rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        if path.name == 'env.py' and 'alembic' in path.parts:
            continue
        text = path.read_text()
        n = len(pattern.findall(text))
        if n > 0:
            rel = path.relative_to(CORE_SRC_ROOT)
            counts[str(rel)] = n
    return counts


def test_single_leader_exemption_only_two_call_sites():
    """AC-X-4 / AC-X-8: exactly 2 pg_try_advisory_lock callers in core (excluding alembic)."""
    counts = _scan_for_pg_try_advisory_lock()
    files = set(counts.keys())
    assert files == EXPECTED_PG_TRY_ADVISORY_LOCK_FILES, (
        f'Unexpected pg_try_advisory_lock call sites: {files}. '
        f'Expected exactly {EXPECTED_PG_TRY_ADVISORY_LOCK_FILES}. '
        'Adding a third caller requires design review (AC-X-4: leader exemption).'
    )


def test_alembic_env_uses_separate_advisory_lock():
    """AC-X-4: migration uses a different lock function + a different lock id space."""
    alembic_env = CORE_SRC_ROOT / 'alembic' / 'env.py'
    if not alembic_env.exists():
        pytest.skip('alembic/env.py not present in this build')
    text = alembic_env.read_text()
    assert 'MIGRATION_LOCK_ID' in text, (
        'alembic/env.py must reference MIGRATION_LOCK_ID — separate id space from F9'
    )
    # The migration uses blocking pg_advisory_lock, not the spin-on pg_try_advisory_lock
    # — they are different SQL functions and the static guard above should not see this.
    assert 'pg_try_advisory_lock' not in text, (
        'alembic/env.py must use pg_advisory_lock (blocking), not pg_try_advisory_lock'
    )
