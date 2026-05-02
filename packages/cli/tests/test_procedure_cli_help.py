"""Help-text fences for the F14 ``memex procedure`` CLI (TC-F14-5).

Locks that:

* ``memex procedure --help`` mentions the ``procedure:<verb>:<context-tag>``
  shape and Memory Worth (so an operator can self-discover the contract).
* Each subcommand (``list`` / ``show`` / ``add``) is reachable via help and
  surfaces the right flag(s).
* ``--history`` flag is documented for ``show`` (back-compat: default
  remains active value).
"""

from __future__ import annotations

import re

from memex_cli.procedure import app


def _normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def test_procedure_top_help_documents_namespace_and_mw_score(runner, strip_ansi):
    """Top-level help describes the procedure: namespace + Memory Worth."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'procedure:<verb>:<context-tag>' in text
    assert 'Memory Worth' in text


def test_procedure_list_help_documents_vault_and_limit(runner, strip_ansi):
    """``memex procedure list --help`` exposes ``--vault`` and ``--limit``."""
    result = runner.invoke(app, ['list', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert '--vault' in text
    assert '--limit' in text
    assert 'Memory Worth' in text


def test_procedure_show_help_documents_history_flag(runner, strip_ansi):
    """``memex procedure show --help`` exposes the ``--history`` flag."""
    result = runner.invoke(app, ['show', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert '--history' in text
    # Either 'envelope' or 'capped' is documented in the flag description.
    assert 'envelope' in text or 'capped' in text


def test_procedure_add_help_describes_versioned_write(runner, strip_ansi):
    """``memex procedure add --help`` describes versioned write semantics."""
    result = runner.invoke(app, ['add', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'versioned' in text
    assert 'history' in text


def test_procedure_show_rejects_non_procedure_keys(runner, strip_ansi):
    """``memex procedure show global:foo`` exits with code 2 (Typer-style misuse)."""
    result = runner.invoke(app, ['show', 'global:not-a-procedure'])
    assert result.exit_code == 2
    text = strip_ansi(result.stdout)
    assert 'Not a procedure key' in text


def test_procedure_add_rejects_non_procedure_keys(runner, strip_ansi):
    """``memex procedure add global:foo body`` rejects misuse before any API call."""
    result = runner.invoke(app, ['add', 'user:not-a-procedure', 'body'])
    assert result.exit_code == 2
    text = strip_ansi(result.stdout)
    assert 'Not a procedure key' in text
