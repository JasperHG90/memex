"""TC-11-5 (revised) + TC-11-6: Tier A seed invariants.

- Runtime advisory-lock count: exactly one MEMEX_LEADER_LOCK_ID call site
  in the runtime path pre-F9. F9 will add exactly one more (entity_lock_id).
  The migration lock at alembic/env.py is excluded by inspection.
- Default-target pin guard: no test fixture invokes alembic upgrade with
  target='head' under Tier A NIE stubs (would crash on revision 025).
"""

from __future__ import annotations

import pathlib as plb


_REPO_ROOT = plb.Path(__file__).resolve().parents[4]
_CORE_RUNTIME = _REPO_ROOT / 'packages' / 'core' / 'src' / 'memex_core'
_TEST_DIRS = [
    _REPO_ROOT / 'tests',
    _REPO_ROOT / 'packages' / 'core' / 'tests',
]


def _mentions_leader_lock_in_code(path: plb.Path) -> bool:
    """True iff the file references MEMEX_LEADER_LOCK_ID on a non-comment line.

    Comment-only mentions (docstrings, ``# … MEMEX_LEADER_LOCK_ID …`` cross-
    references) are not call sites and must not count toward the invariant —
    only api.py / entity_maintenance.py point at it explanatorily; only
    scheduler.py actually defines and acquires the lock.
    """
    for raw in path.read_text(encoding='utf-8').splitlines():
        if 'MEMEX_LEADER_LOCK_ID' not in raw:
            continue
        if raw.lstrip().startswith('#'):
            continue
        return True
    return False


def test_runtime_advisory_lock_invariant_pre_f9() -> None:
    runtime_files = [p for p in _CORE_RUNTIME.rglob('*.py') if 'alembic' not in p.parts]
    leader_sites = [p for p in runtime_files if _mentions_leader_lock_in_code(p)]
    assert len(leader_sites) == 1, (
        f'Expected exactly 1 leader-lock call site pre-F9, got {len(leader_sites)}: '
        f'{[str(p.relative_to(_REPO_ROOT)) for p in leader_sites]}'
    )
    assert leader_sites[0].name == 'scheduler.py'


def test_no_test_fixture_forces_upgrade_to_head() -> None:
    self_path = plb.Path(__file__).resolve()
    offenders: list[str] = []
    for test_dir in _TEST_DIRS:
        if not test_dir.exists():
            continue
        for p in test_dir.rglob('*.py'):
            if '__pycache__' in p.parts or p.resolve() == self_path:
                continue
            text = p.read_text(encoding='utf-8')
            for needle in (
                "target='head'",
                'target="head"',
                "'upgrade', 'head'",
                '"upgrade", "head"',
            ):
                if needle in text:
                    offenders.append(f'{p.relative_to(_REPO_ROOT)} :: {needle!r}')
    assert not offenders, (
        f'Tier A NIE-stubs at revisions 025-029 will crash any fixture that '
        f"upgrades to head; pin to '024_intent_risk_classifier' (or the "
        f"feature-test's own revision). Offenders: {offenders}"
    )


def test_alembic_helper_default_pinned_to_024() -> None:
    helper = _REPO_ROOT / 'packages' / 'core' / 'tests' / 'integration' / '_alembic_test_helpers.py'
    text = helper.read_text(encoding='utf-8')
    assert "target: str = '024_intent_risk_classifier'" in text, (
        '_alembic_test_helpers.alembic_upgrade default target must be pinned to '
        "'024_intent_risk_classifier' under Tier A NIE stubs"
    )
