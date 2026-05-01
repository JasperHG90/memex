"""Unit-level guards for the Tier A seed-PR alembic substrate.

Covers:
- TC-11-1: chain integrity via ScriptDirectory.walk_revisions() (no DB).
- TC-11-2: each 025-029 stub raises NotImplementedError on upgrade()/downgrade().
- TC-11-4: each stub module imports cleanly.
"""

from __future__ import annotations

import importlib.util
import pathlib as plb

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


_PACKAGE_ROOT = plb.Path(__file__).resolve().parents[2] / 'src' / 'memex_core'
_ALEMBIC_INI = _PACKAGE_ROOT / 'alembic.ini'
_VERSIONS_DIR = _PACKAGE_ROOT / 'alembic' / 'versions'

_TIER_A_STUBS: list[tuple[str, str, str]] = [
    # 025_maintenance_proposals (F6) is no longer a stub — see PR #20
    # (real maintenance_proposals table + LintService rule engine).
    # Integration coverage in tests/integration/test_int_f6_rules.py.
    ('026_revisit_columns', '025_maintenance_proposals', 'F20'),
    # 027_consolidation_ticks (F38) is no longer a stub — see PR #19
    # (real consolidation_ticks table + per-tick summary rows).
    # 028_procedure_outcomes (F14) is no longer a stub — see PR #18
    # (real procedure_outcomes table + vault-scoped MW counters).
    ('029_lint_llm_quota', '028_procedure_outcomes', 'F10'),
]


def _load_stub(name: str):
    src = _VERSIONS_DIR / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, src)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_chain_is_linear_and_correct() -> None:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option('script_location', str(_PACKAGE_ROOT / 'alembic'))
    sd = ScriptDirectory.from_config(cfg)

    heads = sd.get_heads()
    assert heads == ['029_lint_llm_quota'], f'Expected single head 029, got {heads}'

    walk = list(sd.walk_revisions())
    top5 = [(r.revision, r.down_revision) for r in walk[:5]]
    expected_top5 = [
        ('029_lint_llm_quota', '028_procedure_outcomes'),
        ('028_procedure_outcomes', '027_consolidation_ticks'),
        ('027_consolidation_ticks', '026_revisit_columns'),
        ('026_revisit_columns', '025_maintenance_proposals'),
        ('025_maintenance_proposals', '024_intent_risk_classifier'),
    ]
    assert top5 == expected_top5, f'Tier A chain mismatch: got {top5}'


@pytest.mark.parametrize('rev,down,fid', _TIER_A_STUBS)
def test_each_stub_raises_not_implemented(rev: str, down: str, fid: str) -> None:
    mod = _load_stub(rev)
    assert mod.revision == rev
    assert mod.down_revision == down
    with pytest.raises(NotImplementedError, match=fid):
        mod.upgrade()
    with pytest.raises(NotImplementedError, match=fid):
        mod.downgrade()


def test_all_stubs_importable() -> None:
    for rev, _down, _fid in _TIER_A_STUBS:
        mod = _load_stub(rev)
        assert hasattr(mod, 'upgrade')
        assert hasattr(mod, 'downgrade')
        assert hasattr(mod, 'revision')
        assert hasattr(mod, 'down_revision')
