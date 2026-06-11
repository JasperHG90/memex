"""Tests for the ``memex agent-surface`` CLI subcommand.

Pins:
- Each composition target emits non-empty, deterministic output.
- ``agent-surface universal`` and ``agent-surface mcp`` produce different
  content (mcp is intentionally minimal transport-only).
- ``agent-surface hermes`` and ``agent-surface claude-code`` include the
  universal block.
- ``agent-surface hermes`` and ``agent-surface claude-code`` include the
  procedural-plane doctrine block (the routing rules for the 8
  ``memex_procedural_*`` tools) — the CLI bridge must match what the
  in-process Hermes plugin path ships.
- ``agent-surface universal`` and ``agent-surface mcp`` MUST NOT include
  the procedural doctrine (opt-in: callers without procedural tools would burn
  the budget for no behavioural gain).
- ``--output-format=json`` wraps the content in the Claude Code SessionStart
  envelope ``{"systemPromptAdditions": "..."}`` so the hook can pipe it
  directly through.
- ``--output-dir DIR`` writes ``<DIR>/memex-agent-surface.md`` atomically,
  skipping the rewrite when content is unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from memex_cli.agent_surface import _OUTPUT_FILENAME, app

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
    """``agent-surface generic`` is an alias for ``agent-surface universal``."""
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


def test_mcp_target_is_terse_and_pointer_at_agent_surface() -> None:
    """``agent-surface mcp`` is the Tier 1a transport surface — minimal,
    points at `agent_surface` for composition rules. Pin both positive
    content (progressive disclosure, vault defaults, pointer) and negative
    (Tier 1b content absent)."""
    out = _run('mcp')
    # Positive: load-bearing transport facts must be present.
    assert 'TOOL DISCOVERY' in out
    assert 'memex_tags' in out
    assert 'memex_search' in out
    assert 'memex_get_schema' in out
    assert 'VAULT DEFAULTS' in out
    assert 'agent_surface' in out
    # Negative: Tier 1b CONTENT must NOT appear here (the pointer naming
    # "5-step resolution flow" is contrastive — that's fine; what we ban is
    # the actual scaffolding like "Option A"/"Option B").
    assert 'Option A' not in out
    assert 'Option B' not in out
    assert 'Disambiguate —' not in out


def test_mcp_target_is_identical_to_mcp_server_instructions() -> None:
    """``agent-surface mcp`` output must be the same string as
    ``memex_mcp.server.mcp.instructions`` — the CLI surface is a debug
    inspection of exactly what MCP serves, not a separate rendering."""
    from memex_common.agent_surface import MCP_TRANSPORT_INSTRUCTIONS

    out = _run('mcp')
    assert out == MCP_TRANSPORT_INSTRUCTIONS, (
        'agent-surface mcp output drifted from the SSOT. Both the CLI and '
        'the MCP server import from `memex_common.agent_surface.MCP_TRANSPORT_INSTRUCTIONS`.'
    )


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


# ---------------------------------------------------------------------------
# --output-dir: file-sink behavior for the Claude Code plugin install path.
# ---------------------------------------------------------------------------


def test_output_dir_writes_named_file(tmp_path: Path) -> None:
    """`--output-dir DIR` writes <DIR>/memex-agent-surface.md with the same
    body that stdout mode would emit."""
    result = runner.invoke(app, ['claude-code', '--output-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    written = (tmp_path / _OUTPUT_FILENAME).read_text(encoding='utf-8')
    expected = _run('claude-code')
    assert written == expected


def test_output_dir_creates_missing_parent(tmp_path: Path) -> None:
    """Nested directories are created as needed (mkdir parents=True)."""
    nested = tmp_path / 'deep' / 'nested' / 'rules'
    assert not nested.exists()
    result = runner.invoke(app, ['claude-code', '--output-dir', str(nested)])
    assert result.exit_code == 0, result.output
    assert (nested / _OUTPUT_FILENAME).is_file()


def test_output_dir_skips_rewrite_on_unchanged_content(tmp_path: Path) -> None:
    """Second invocation with identical args must NOT rewrite the file —
    mtime stays put so file-watchers and InstructionsLoaded don't refire."""
    args = ['claude-code', '--output-dir', str(tmp_path)]
    runner.invoke(app, args)
    target = tmp_path / _OUTPUT_FILENAME
    first_mtime_ns = target.stat().st_mtime_ns
    # Force a measurable mtime delta even on filesystems with coarse mtime.
    os.utime(target, ns=(first_mtime_ns, first_mtime_ns - 1_000_000_000))
    sentinel_mtime_ns = target.stat().st_mtime_ns

    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert target.stat().st_mtime_ns == sentinel_mtime_ns, (
        'content-diff skip failed: file was rewritten even though body was unchanged'
    )


def test_output_dir_rewrites_on_content_diff(tmp_path: Path) -> None:
    """If on-disk content has drifted from what the CLI would emit, the next
    invocation must restore it."""
    args = ['claude-code', '--output-dir', str(tmp_path)]
    runner.invoke(app, args)
    target = tmp_path / _OUTPUT_FILENAME
    target.write_text('CORRUPTED — should be restored on next call', encoding='utf-8')

    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    restored = target.read_text(encoding='utf-8')
    assert restored == _run('claude-code')


def test_output_dir_atomic_no_partial_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the atomic rename fails mid-write, no .tmp litter should remain
    and any pre-existing target file must be untouched."""
    args = ['claude-code', '--output-dir', str(tmp_path)]
    # Prime an existing file, then drift its content so the next invocation
    # is forced past the content-diff skip and into the write path.
    runner.invoke(app, args)
    target = tmp_path / _OUTPUT_FILENAME
    sentinel = 'PRE-EXISTING CONTENT — must survive a failed rename'
    target.write_text(sentinel, encoding='utf-8')

    import memex_cli.agent_surface as mod

    def _boom(*_args: object, **_kw: object) -> None:
        raise OSError('simulated rename failure')

    monkeypatch.setattr(mod.os, 'replace', _boom)

    result = runner.invoke(app, args)
    assert result.exit_code != 0, 'CLI should propagate the rename failure'
    # Original (sentinel) content intact — no partial overwrite.
    assert target.read_text(encoding='utf-8') == sentinel
    # No leftover temp files.
    tmp_litter = [p for p in tmp_path.iterdir() if p.name.startswith(f'.{_OUTPUT_FILENAME}.')]
    assert tmp_litter == [], f'temp files leaked after failure: {tmp_litter}'


def test_output_dir_with_json_format_errors(tmp_path: Path) -> None:
    """`--output-dir` with `--output-format=json` is contradictory — the JSON
    envelope is only meaningful for stdout piping. Fail loudly rather than
    silently dropping one of the flags."""
    result = runner.invoke(
        app,
        ['claude-code', '--output-dir', str(tmp_path), '--output-format', 'json'],
    )
    assert result.exit_code != 0, 'JSON + output-dir must be rejected'
    combined = (result.output or '') + (getattr(result, 'stderr', '') or '')
    assert 'output-dir' in combined.lower() and 'json' in combined.lower()
    assert not (tmp_path / _OUTPUT_FILENAME).exists(), (
        'rejected invocation should not have written the file'
    )


def test_output_dir_emits_nothing_to_stdout(tmp_path: Path) -> None:
    """When `--output-dir` is set the markdown body lives on disk, not on
    stdout — silence keeps the SessionStart hook's JSON envelope clean."""
    result = runner.invoke(app, ['claude-code', '--output-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.stdout == '', f'stdout should be empty, got: {result.stdout!r}'


def test_critical_constraint_xml_tags_in_universal() -> None:
    """Load-bearing CRITICAL_HEADER / VIRTUAL_UNIT / CRITICAL_FOOTER content
    is wrapped in ``<critical_constraint name="…">`` / ``<critical_reminder>``
    XML blocks per Anthropic best practice. Pin the named tags so a
    regression that drops the structure trips here."""
    out = _run('universal')
    # CRITICAL_HEADER → 4 named constraints. The observation-read-only
    # constraint was previously named `virtual_unit_404` when the server
    # returned 404 on observation deprio; it now returns HTTP 400 and the
    # constraint name reflects the actual semantic (read-only projection)
    # rather than the status code.
    for name in (
        'record_outcome_shape',
        'observation_read_only',
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


# ---------------------------------------------------------------------------
# Procedural-plane doctrine — opt-in for the agentic profiles.
# Pin presence in `hermes` and `claude-code` (the agentic surfaces) and
# absence in `universal` and `mcp` (the terse / transport surfaces).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('profile', ['hermes', 'claude-code'])
def test_agentic_profile_includes_procedural_doctrine(profile: str) -> None:
    """The two agentic surfaces (Hermes plugin path and Claude Code
    SessionStart hook) MUST ship the procedural-plane doctrine. The
    doctrine is the routing rule that drives write intents
    ``"this is how to do X"`` to the procedural plane — without it,
    agents silently fall back to ``memex_add_note`` (note plane) or
    ``memex_kv_put`` (KV plane), both of which are wrong for the
    procedural use case.
    """
    out = _run(profile)
    # Identity marker of the procedural block.
    assert '## Procedural plane' in out
    # The two procedural kinds — cases are NOTES via memex_case_submit.
    assert '`procedure`' in out
    assert '`strategy`' in out
    assert 'memex_case_submit' in out
    # Identity-anchor rule (UNIQUE on (kind, scope, verb, context)).
    assert '(kind, scope, verb, context)' in out
    # At least one tool name from the procedural surface.
    assert 'memex_procedural_create' in out
    # The agent-facing briefing tool is gone — cards arrive in the
    # session briefing (JG decision 2026-06-10).
    assert 'memex_procedural_briefing_cards' not in out


@pytest.mark.parametrize('profile', ['universal', 'mcp'])
def test_terse_profile_does_not_include_procedural_doctrine(profile: str) -> None:
    """The terse / transport profiles MUST NOT ship the procedural doctrine.
    ``universal`` is for agents with no procedural tools; ``mcp``
    is transport-only (Tier 1a). Both would burn ~1,750 chars on
    routing rules the consumer cannot act on.
    """
    out = _run(profile)
    assert '## Procedural plane' not in out
    assert 'memex_procedural_create' not in out
    assert 'memex_procedural_briefing_cards' not in out


def test_hermes_profile_uses_compose_with_procedural_not_universal() -> None:
    """Defence-in-depth trip-wire: a regression that swaps
    ``compose_with_procedural()`` back to ``compose_universal()`` in
    ``_compose_for_target`` would still pass the universal-block
    presence tests but silently drop the procedural doctrine. This test pins
    the procedural heading directly — the swap would surface here.
    """
    out = _run('hermes')
    assert '## Procedural plane' in out


def test_claude_code_profile_uses_compose_with_procedural_not_universal() -> None:
    """Same as the hermes trip-wire, for the claude-code target. The
    SessionStart hook reads this string verbatim — if the procedural
    block is missing, every Claude Code session silently misroutes
    write intents to the wrong plane.
    """
    out = _run('claude-code')
    assert '## Procedural plane' in out


def test_hermes_profile_composition_order() -> None:
    """The composition order is universal → procedural → harness.
    Pin the order by checking the procedural heading's offset
    relative to the universal block's footer marker."""
    out = _run('hermes')
    universal_footer = '## Critical reminders'
    procedural_heading = '## Procedural plane'
    assert universal_footer in out
    assert procedural_heading in out
    assert out.index(universal_footer) < out.index(procedural_heading)


def test_claude_code_profile_composition_order() -> None:
    """Same composition order trip-wire as the hermes profile — both
    agentic surfaces use the same ``compose_with_procedural()`` +
    harness composition shape, so the order is identical."""
    out = _run('claude-code')
    universal_footer = '## Critical reminders'
    procedural_heading = '## Procedural plane'
    assert universal_footer in out
    assert procedural_heading in out
    assert out.index(universal_footer) < out.index(procedural_heading)
