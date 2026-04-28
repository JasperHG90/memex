"""Regression fences for the `memex kv` CLI help text.

These tests guard against the OrangeHermes-class bug from regressing in CLI
prose. Earlier versions described KV as a "fact store (lightweight structured
memory)" — language that conflated KV with the memory graph and invited
agents to write learned-fact summaries into KV instead of retaining notes.
"""

from __future__ import annotations

import re

from memex_cli.kv import app


def _normalize(text: str) -> str:
    """Collapse Typer's terminal-wrapped whitespace into single spaces.

    Typer renders help text into a fixed-width box and wraps long phrases
    across lines (sometimes through ``│`` column borders). Normalizing
    whitespace lets us assert on token sequences without depending on the
    runner's terminal width.
    """
    return re.sub(r'\s+', ' ', text).strip()


def test_kv_help_no_longer_calls_kv_a_fact_store(runner, strip_ansi):
    """The top-level `memex kv` help must not advertise KV as a fact store."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'fact store' not in text
    assert 'lightweight structured memory' not in text


def test_kv_help_routes_learned_facts_to_notes(runner, strip_ansi):
    """The top-level help must redirect 'learned content' away from KV."""
    result = runner.invoke(app, ['--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'memex note add' in text


def test_kv_write_help_drops_bare_fact_wording(runner, strip_ansi):
    """`memex kv write --help` must not call the value 'a fact'."""
    result = runner.invoke(app, ['write', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    # The argument was previously "The fact/value to store." — must be gone.
    assert 'fact/value' not in text
    # Docstring header was previously "Write a fact to the KV store."
    assert 'Write a fact' not in text


def test_kv_get_help_drops_bare_fact_wording(runner, strip_ansi):
    result = runner.invoke(app, ['get', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'Get a fact' not in text


def test_kv_search_help_drops_bare_fact_wording(runner, strip_ansi):
    result = runner.invoke(app, ['search', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout)).lower()
    assert 'search facts' not in text


def test_kv_list_help_drops_bare_fact_wording(runner, strip_ansi):
    result = runner.invoke(app, ['list', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'List all facts' not in text


def test_kv_delete_help_drops_bare_fact_wording(runner, strip_ansi):
    result = runner.invoke(app, ['delete', '--help'])
    assert result.exit_code == 0
    text = _normalize(strip_ansi(result.stdout))
    assert 'Delete a fact' not in text
