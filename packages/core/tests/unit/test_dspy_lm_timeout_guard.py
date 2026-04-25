"""CI grep guard: every ``dspy.LM(...)`` site must pass ``timeout=`` (AC-006 part b).

PR #43 (commit ``3951edf``) wired ``timeout=model_config.timeout`` at all six
existing ``dspy.LM(...)`` construction sites. This test catches a future PR
adding a *new* ``dspy.LM(...)`` site without the kwarg, which would re-introduce
the wedge mode (a hung request without a socket deadline can pin a worker
indefinitely under memory pressure — see issue #50).

Approach: regex grep on the source tree, then for each match grab the next
8 lines and check that ``timeout=`` is somewhere in that block. The 8-line
window is required because ``_make_lm`` (``extraction/engine.py:106``) splits
the construction across multiple lines.

Regex-based, not AST-based — keep it simple per AC-006.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import memex_core


def _core_src_root() -> Path:
    """Resolve the ``memex_core`` source tree, regardless of cwd.

    Avoids hardcoding ``packages/core/src/memex_core`` so the test still works
    when invoked from any directory (e.g. ``cd packages/core && pytest ...``).
    """
    return Path(memex_core.__file__).parent


def test_all_dspy_lm_constructions_pass_timeout() -> None:
    root = _core_src_root()
    result = subprocess.run(
        ['grep', '-rn', '-A', '8', 'dspy.LM(', str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    # grep exits 1 when no matches; that would mean we have no dspy.LM sites
    # at all, which itself is a regression worth surfacing.
    assert result.returncode == 0, (
        f'grep found no dspy.LM(...) sites under {root}. stderr: {result.stderr!r}'
    )

    blocks = result.stdout.split('\n--\n')
    missing: list[str] = []
    for block in blocks:
        if not block.strip() or 'dspy.LM(' not in block:
            continue
        # Skip self-reference inside this guard test or the plumbing test.
        if 'test_dspy_lm_timeout' in block.split('\n', 1)[0]:
            continue
        if 'timeout=' not in block:
            first_line = block.split('\n', 1)[0]
            missing.append(first_line)

    assert not missing, (
        'dspy.LM(...) construction sites without timeout= kwarg in surrounding '
        '8 lines:\n  '
        + '\n  '.join(missing)
        + '\nWiring timeout=model_cfg.timeout at every dspy.LM(...) is required '
        'so the underlying httpx client gets a socket deadline (see AC-006, '
        'RFC-001 §1.5(b), and packages/core/tests/unit/test_dspy_lm_timeout_plumbing.py).'
    )
