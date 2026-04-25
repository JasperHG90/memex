"""CI grep guard: every ``dspy.LM(...)`` site must pass ``timeout=`` (AC-006 part b).

PR #43 (commit ``3951edf``) wired ``timeout=model_config.timeout`` at the six
``dspy.LM(...)`` sites in ``packages/core``. **Phase 3 adversarial review (F5)
caught a scope gap**: the original guard only walked ``memex_core``, missing
real ``dspy.LM(...)`` constructions in ``packages/eval/src/memex_eval/judge.py``
and ``packages/cli/src/memex_cli/memory.py``. The guard now scans every
``packages/*/src/`` tree (core, cli, eval, common — common may have none, but
the loop is uniform). Tests directories remain out-of-scope: a hung LM in a
test is bounded by ``pytest-timeout``, not by ``dspy.LM(timeout=)``.

This test catches a future PR adding a *new* ``dspy.LM(...)`` site without the
kwarg, which would re-introduce the wedge mode (a hung request without a
socket deadline can pin a worker indefinitely under memory pressure — see
issue #50).

Approach: regex grep on each package's ``src`` tree, then for each match grab
the next 8 lines and check that ``timeout=`` is somewhere in that block. The
8-line window is required because ``_make_lm`` (``extraction/engine.py:106``)
splits the construction across multiple lines.

Regex-based, not AST-based — keep it simple per AC-006.

Known limitation (F21 in Phase 3 review): a ``timeout=`` token appearing in a
comment within the 8-line window would be a false-negative. The guard treats
the surrounding text uniformly. AST-based scanning (``ast.parse``, walk
``Call`` nodes whose ``func.attr == 'LM'``) is the long-term fix and would
also obviate the 8-line window heuristic; deferred since the false-negative
risk is low (no current site puts ``timeout=`` in comments).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import memex_core


def _packages_src_roots() -> list[Path]:
    """Return every ``packages/*/src/`` directory in the monorepo.

    F5 widening: scans all packaged source trees, not just ``memex_core``.
    Excludes test directories — a hung LM in a test is bounded by
    ``pytest-timeout``, not by ``dspy.LM(timeout=)``.

    Walks up from ``memex_core.__file__`` (which is in
    ``packages/core/src/memex_core/__init__.py``) to find the workspace root,
    then enumerates ``packages/*/src``.
    """
    # memex_core.__file__ → .../packages/core/src/memex_core/__init__.py
    # parents: [memex_core] → [src] → [core] → [packages] → [workspace_root]
    workspace_root = Path(memex_core.__file__).parents[4]
    packages_dir = workspace_root / 'packages'
    assert packages_dir.is_dir(), (
        f'expected packages/ at workspace root {workspace_root}; '
        f'walked from memex_core.__file__={memex_core.__file__}'
    )
    src_roots: list[Path] = []
    for pkg in sorted(packages_dir.iterdir()):
        src = pkg / 'src'
        if src.is_dir():
            src_roots.append(src)
    return src_roots


def test_all_dspy_lm_constructions_pass_timeout() -> None:
    src_roots = _packages_src_roots()
    assert src_roots, 'no packages/*/src directories found — workspace layout broken?'

    missing: list[str] = []
    found_any_site = False

    for root in src_roots:
        result = subprocess.run(
            ['grep', '-rn', '-A', '8', 'dspy.LM(', str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        # grep exit code 1 = no matches in this package; that's fine — eval may
        # have nothing, common may have nothing. Only exit codes >=2 are errors.
        if result.returncode >= 2:
            raise AssertionError(
                f'grep failed under {root} with exit {result.returncode}; stderr: {result.stderr!r}'
            )
        if not result.stdout.strip():
            continue

        blocks = result.stdout.split('\n--\n')
        for block in blocks:
            if not block.strip() or 'dspy.LM(' not in block:
                continue
            # Skip self-reference inside this guard test or the plumbing test
            # (only relevant if a future refactor moves them under packages/*/src).
            if 'test_dspy_lm_timeout' in block.split('\n', 1)[0]:
                continue
            found_any_site = True
            if 'timeout=' not in block:
                first_line = block.split('\n', 1)[0]
                missing.append(first_line)

    assert found_any_site, (
        f'grep found no dspy.LM(...) sites across {len(src_roots)} package src trees. '
        f'Either dspy.LM is no longer used (regression worth surfacing) or the '
        f'walk in _packages_src_roots() broke. Roots scanned: '
        + ', '.join(str(r) for r in src_roots)
    )

    assert not missing, (
        'dspy.LM(...) construction sites without timeout= kwarg in surrounding '
        '8 lines:\n  '
        + '\n  '.join(missing)
        + '\nWiring timeout= at every dspy.LM(...) is required so the '
        'underlying httpx client gets a socket deadline (see AC-006, '
        'RFC-001 §1.5(b), and packages/core/tests/unit/test_dspy_lm_timeout_plumbing.py). '
        'F5 widened the scope to all packages/*/src/ in Phase 3 rework.'
    )
