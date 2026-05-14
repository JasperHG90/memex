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

import pytest
from typer.testing import CliRunner

from memex_cli.agent_surface import app

runner = CliRunner()


def _run(*args: str) -> str:
    """Invoke the CLI with args; assert success AND empty stderr; return stdout.

    Stderr separation matters because the JSON-envelope output mode is
    meant for piping into ``jq`` / the Claude Code SessionStart hook —
    any warning text leaking to stderr while the command exit-codes 0
    would silently corrupt the consumer."""
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, f'CLI failed: {result.output!r}'
    # `result.stderr` is empty by default in click ≥8.2 (typer ≥0.16);
    # the assertion catches future regressions that echo to err.
    stderr = getattr(result, 'stderr', '') or ''
    assert stderr == '', f'CLI emitted stderr: {stderr!r}'
    return result.stdout


def test_universal_profile_emits_universal_block() -> None:
    out = _run('universal')
    assert '## Critical constraints' in out
    assert 'units=[{unit_id, verb, reason}]' in out
    assert 'scope qualifier' in out


def test_generic_alias_matches_universal() -> None:
    """``--for=generic`` is an alias for ``--for=universal``."""
    universal = _run('universal')
    generic = _run('generic')
    assert universal == generic


def test_hermes_profile_includes_universal_block_and_harness() -> None:
    out = _run('hermes')
    # Universal block markers
    assert '## Critical constraints' in out
    # Hermes-specific harness markers
    assert 'Hermes-specific framing' in out
    assert 'that worked' in out


def test_claude_code_profile_includes_universal_block_and_harness() -> None:
    out = _run('claude-code')
    # Universal block markers
    assert '## Critical constraints' in out
    # Claude Code-specific markers
    assert 'Claude Code-specific framing' in out
    assert 'author="claude-code"' in out
    assert '/remember' in out
    assert '/recall' in out


def test_mcp_profile_is_terse_and_pointer_at_agent_surface() -> None:
    """``--for=mcp`` is the Tier 1a transport surface — minimal, points at
    `agent_surface` for composition rules. Pin both positive content
    (progressive disclosure, vault defaults, pointer) and negative
    (Tier 1b content absent)."""
    out = _run('mcp')
    # Positive: load-bearing transport facts must be present.
    assert 'Progressive disclosure' in out
    assert 'memex_tags' in out
    assert 'memex_search' in out
    assert 'memex_get_schema' in out
    assert 'Vault defaults' in out
    assert 'agent_surface' in out
    # Negative: Tier 1b content must NOT appear here.
    assert 'Options A/B/C' not in out
    assert '5-step' not in out


@pytest.mark.parametrize('profile', ['universal', 'generic', 'hermes', 'claude-code', 'mcp'])
def test_profile_output_is_deterministic(profile: str) -> None:
    """Every profile must emit byte-equal output across invocations —
    cacheable-prefix invariant per dbreunig's Claude Code cache analysis."""
    a = _run(profile)
    b = _run(profile)
    assert a == b, f'profile {profile!r} is non-deterministic: differs by {len(a) - len(b)} chars'


def test_json_output_wraps_content_in_session_start_envelope() -> None:
    """JSON mode emits ``{"systemPromptAdditions": "..."}`` for Claude Code
    SessionStart hooks to pipe through directly."""
    out = _run('claude-code', '--output-format', 'json')
    payload = json.loads(out)
    assert 'systemPromptAdditions' in payload
    body = payload['systemPromptAdditions']
    assert '## Critical constraints' in body
    assert 'Claude Code-specific framing' in body


def test_text_output_is_raw_markdown() -> None:
    """Default ``--output-format=text`` emits raw markdown (no JSON wrapping)."""
    out = _run('universal')
    # Should not be a JSON object
    assert not out.lstrip().startswith('{')
    assert '## Critical constraints' in out


def test_target_is_required_positional() -> None:
    """The CLI used to accept bare ``memex agent-surface`` (defaulted to
    universal). The positional ``target`` arg now makes it required so
    every caller declares intent. Bare invocation must fail with non-zero
    exit code."""
    result = runner.invoke(app, [])
    assert result.exit_code != 0, (
        f'bare invocation should fail (target required); got exit={result.exit_code}'
    )


def test_critical_constraint_xml_tags_in_universal() -> None:
    """Load-bearing CRITICAL_HEADER / VIRTUAL_UNIT / CRITICAL_FOOTER content
    is wrapped in ``<critical_constraint name="…">`` / ``<critical_reminder>``
    XML blocks per Anthropic best practice. Pin the named tags so a
    regression that drops the structure trips here."""
    out = _run('universal')
    # CRITICAL_HEADER → 4 named constraints
    for name in (
        'record_outcome_shape',
        'virtual_unit_404',
        'kv_scope_qualifier',
        'citations_required',
    ):
        assert f'<critical_constraint name="{name}">' in out, (
            f'missing <critical_constraint name="{name}"> tag in universal output'
        )
    # VIRTUAL_UNIT → 1 named constraint (the long-form one)
    assert '<critical_constraint name="virtual_unit_filter">' in out
    # CRITICAL_FOOTER → 4 named reminders
    for name in (
        'record_outcome_shape',
        'virtual_unit_filter',
        'kv_scope_qualifier',
        'citations_required',
    ):
        assert f'<critical_reminder name="{name}">' in out, (
            f'missing <critical_reminder name="{name}"> tag in universal output'
        )
