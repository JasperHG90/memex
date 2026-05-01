"""Help-text fences for the F38 ``memex consolidate`` CLI (TC-F38-CLI).

Locks that the operator-facing surface advertised by RFC-010 actually
exists and exposes the documented flags:

* Top-level help mentions the orchestration ordering (contradiction →
  reflection → prune-stale-only) so an operator self-discovers the
  contract.
* Each subcommand (``tick`` / ``status``) is reachable via help and
  surfaces the right flag(s) — ``--vault`` / ``--dry-run`` / ``--budget``
  on tick, ``--vault`` on status.
"""

from __future__ import annotations

import re

from memex_cli.consolidate import app


def _normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def test_consolidate_top_help_documents_step_ordering(runner, strip_ansi):
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'contradiction' in text and 'reflection' in text and 'prune' in text


def test_consolidate_tick_help_exposes_dry_run_and_budget(runner, strip_ansi):
    result = runner.invoke(app, ['tick', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert '--vault' in text
    assert '--dry-run' in text
    assert '--budget' in text


def test_consolidate_status_help_exposes_vault_filter(runner, strip_ansi):
    result = runner.invoke(app, ['status', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert '--vault' in text


def test_consolidate_subcommands_listed(runner, strip_ansi):
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = strip_ansi(result.stdout)
    assert 'tick' in text
    assert 'status' in text
