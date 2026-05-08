"""TC-11-5 (revised) + TC-11-6: Tier A seed invariants.

- Default-target pin guard: no test fixture invokes alembic upgrade with
  target='head' under Tier A NIE stubs (would crash on revision 025).

The pre-F9 leader-lock-call-site invariant has been retired now that F9
has shipped — ``packages/core/src/memex_core/api.py`` carries an
explanatory comment about the leader lock alongside ``scheduler.py``'s
canonical ``MEMEX_LEADER_LOCK_ID`` definition, so a string-grep count of
"== 1" is no longer the right shape for the invariant.
"""

from __future__ import annotations

import pathlib as plb


_REPO_ROOT = plb.Path(__file__).resolve().parents[4]
_TEST_DIRS = [
    _REPO_ROOT / 'tests',
    _REPO_ROOT / 'packages' / 'core' / 'tests',
]


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
