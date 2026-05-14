"""Tests for the ``memex agent-surface`` CLI subcommand.

Pins:
- Each composition profile emits non-empty, deterministic output.
- ``--for=universal`` and ``--for=mcp`` produce different content
  (mcp is intentionally minimal transport-only).
- ``--for=hermes`` and ``--for=claude-code`` include the universal block.
- ``--output-format=json`` wraps the content in the Claude Code SessionStart
  envelope ``{"systemPromptAdditions": "..."}`` so the hook can pipe it
  directly through.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from memex_cli.agent_surface import app

runner = CliRunner()


def _run(*args: str) -> str:
    """Invoke the CLI with args, asserting success, and return stdout."""
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, f'CLI failed: {result.output!r}'
    return result.output


def test_universal_profile_emits_universal_block() -> None:
    out = _run('--for', 'universal')
    assert '## Critical constraints' in out
    assert 'units=[{unit_id, verb, reason}]' in out
    assert 'scope qualifier' in out


def test_generic_alias_matches_universal() -> None:
    """``--for=generic`` is an alias for ``--for=universal``."""
    universal = _run('--for', 'universal')
    generic = _run('--for', 'generic')
    assert universal == generic


def test_hermes_profile_includes_universal_block_and_harness() -> None:
    out = _run('--for', 'hermes')
    # Universal block markers
    assert '## Critical constraints' in out
    # Hermes-specific harness markers
    assert 'Hermes-specific framing' in out
    assert 'that worked' in out


def test_claude_code_profile_includes_universal_block_and_harness() -> None:
    out = _run('--for', 'claude-code')
    # Universal block markers
    assert '## Critical constraints' in out
    # Claude Code-specific markers
    assert 'Claude Code-specific framing' in out
    assert 'author="claude-code"' in out
    assert '/remember' in out
    assert '/recall' in out


def test_mcp_profile_is_terse_and_pointer_at_agent_surface() -> None:
    """``--for=mcp`` is the Tier 1a transport surface — minimal, points at
    `agent_surface` for composition rules."""
    out = _run('--for', 'mcp')
    assert 'transport facts' in out
    assert 'agent_surface' in out
    # Tier 1b content must NOT appear here
    assert 'Options A/B/C' not in out
    assert '5-step' not in out


def test_universal_output_is_deterministic() -> None:
    """Same flags → byte-equal output (cacheable prefix invariant)."""
    a = _run('--for', 'universal')
    b = _run('--for', 'universal')
    assert a == b


def test_json_output_wraps_content_in_session_start_envelope() -> None:
    """JSON mode emits ``{"systemPromptAdditions": "..."}`` for Claude Code
    SessionStart hooks to pipe through directly."""
    out = _run('--for', 'claude-code', '--output-format', 'json')
    payload = json.loads(out)
    assert 'systemPromptAdditions' in payload
    body = payload['systemPromptAdditions']
    assert '## Critical constraints' in body
    assert 'Claude Code-specific framing' in body


def test_text_output_is_raw_markdown() -> None:
    """Default ``--output-format=text`` emits raw markdown (no JSON wrapping)."""
    out = _run('--for', 'universal')
    # Should not be a JSON object
    assert not out.lstrip().startswith('{')
    assert '## Critical constraints' in out
