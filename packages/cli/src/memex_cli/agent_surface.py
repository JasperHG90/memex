"""``memex agent-surface`` — emit the agent-facing system-prompt content
for embedding in non-Python agent harnesses (Claude Code SessionStart hook,
arbitrary MCP hosts, etc.).

This is the CLI bridge for non-in-process consumers of the
``memex_common.agent_surface`` SSOT. Python-resident agents (Hermes,
``memex-eval``'s answer modes) import ``compose_universal()`` directly; the
Claude Code plugin and any future shell-hosted agent invokes this command
from a SessionStart hook and pipes the output into the agent system prompt.

Output modes:
- ``--output-format=text`` (default): plain markdown — the composed system-
  prompt content. Suitable for piping into any harness that accepts raw text.
- ``--output-format=json``: a JSON envelope ``{"systemPromptAdditions": "..."}``
  per Claude Code's SessionStart-hook contract.

Targets (positional, required):
- ``universal`` / ``generic`` (alias): only the Tier 1b universal block.
- ``hermes``: Tier 1b + the hermes-only Tier 2 harness (outcome lexicon +
  capture cadence). For environments embedding Hermes without going through
  the in-process plugin path.
- ``claude-code``: Tier 1b + Claude-Code-specific Tier 2 framing (capture
  cadence with ``author: "claude-code"``, slash commands, plugin
  prohibitions). Default surface for the Claude Code SessionStart hook.
- ``mcp``: minimal — transport facts + a pointer at ``agent_surface``. This
  is what the MCP ``instructions=`` field carries; the CLI exposes it for
  inspection / debugging.

The output is deterministic: same args → byte-equal output. This is what
lets the prompt-prefix cache (per dbreunig's Claude Code analysis) survive
across sessions.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from memex_common.agent_harnesses import CLAUDE_CODE_HARNESS, HERMES_HARNESS
from memex_common.agent_surface import (
    MCP_TRANSPORT_INSTRUCTIONS,
    compose_universal,
    compose_with_procedural,
)

# Stable filename for `--output-dir` writes. Owned by the CLI so callers
# (the Claude Code plugin hook in particular) cannot pick a name that
# later renames break installs for existing users.
_OUTPUT_FILENAME = 'memex-agent-surface.md'


app = typer.Typer(
    name='agent-surface',
    help='Emit Memex agent-surface system-prompt content for embedding in agent harnesses.',
    no_args_is_help=True,
)


class Target(str, Enum):
    universal = 'universal'
    generic = 'generic'  # alias for universal
    hermes = 'hermes'
    claude_code = 'claude-code'
    mcp = 'mcp'


class OutputFormat(str, Enum):
    text = 'text'
    json = 'json'


def _compose_for_target(target: Target) -> str:
    if target in (Target.universal, Target.generic):
        return compose_universal()
    if target == Target.hermes:
        # Tier 1b universal + V7 procedural-plane doctrine + Hermes harness.
        # The procedural block ships the routing rules for the 8
        # ``memex_procedural_*`` MCP tools. Non-Python agent harnesses
        # embedding Hermes go through this CLI bridge; in-process Hermes
        # consumers use the same SSOT in ``memex_hermes_plugin.briefing``.
        return compose_with_procedural() + '\n\n' + HERMES_HARNESS
    if target == Target.claude_code:
        # Same surface the SessionStart hook reads — the V7 procedural
        # block is appended to every Memex-aware Claude Code session,
        # matching what the in-process plugin path ships to Hermes.
        return compose_with_procedural() + '\n\n' + CLAUDE_CODE_HARNESS
    if target == Target.mcp:
        # Same SSOT object as ``memex_mcp.server.mcp.instructions`` — the CLI
        # ``mcp`` target is a debug-time inspection of exactly that string.
        return MCP_TRANSPORT_INSTRUCTIONS
    raise ValueError(f'unknown target: {target!r}')


def _write_to_dir(body: str, output_dir: Path) -> None:
    """Atomic write of ``body`` to ``<output_dir>/memex-agent-surface.md``.

    Creates ``output_dir`` if missing. Skips the rewrite when the existing
    file content already matches ``body`` byte-for-byte — avoids mtime churn
    that fires ``InstructionsLoaded`` and any file-watcher hooks, and keeps
    git dirty-state honest when the rules dir lives inside a repo.
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:
        typer.echo(
            f'--output-dir {output_dir} is not a directory ({exc}).',
            err=True,
        )
        raise typer.Exit(code=2) from exc
    target_path = output_dir / _OUTPUT_FILENAME
    if target_path.is_file():
        try:
            if target_path.read_text(encoding='utf-8') == body:
                return
        except (OSError, UnicodeDecodeError):
            # Corrupted or binary on-disk content — fall through to rewrite.
            pass

    fd, tmp_name = tempfile.mkstemp(
        dir=str(output_dir),
        prefix=f'.{_OUTPUT_FILENAME}.',
        suffix='.tmp',
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(body)
        os.replace(tmp_name, target_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


# `app.command()` with `no_args_is_help=True` on the single-command app makes
# typer flatten this into `memex agent-surface TARGET [OPTIONS]`. The function
# name controls the flattened command label, so it must stay as
# `agent_surface` (→ `agent-surface`) to match the LAZY_SUBCOMMANDS key in
# memex_cli.utils. Renaming will break top-level dispatch in LazyTyperGroup.
@app.command()
def agent_surface(
    target: Target = typer.Argument(
        ...,
        help='Composition target: universal/generic, hermes, claude-code, mcp.',
        case_sensitive=False,
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.text,
        '--output-format',
        help='Output format: text (raw markdown) or json (Claude Code SessionStart envelope).',
        case_sensitive=False,
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        '--output-dir',
        '-d',
        help=(
            'If set, write to <dir>/memex-agent-surface.md atomically instead of '
            'stdout. Creates <dir> if missing. Skips the rewrite when content is '
            'unchanged. Mutually exclusive with --output-format=json.'
        ),
    ),
) -> None:
    """Emit the system-prompt content for the requested target to stdout."""
    body = _compose_for_target(target)
    if output_dir is not None:
        if output_format == OutputFormat.json:
            typer.echo(
                '--output-dir is mutually exclusive with --output-format=json '
                '(the JSON envelope is only meaningful for stdout piping).',
                err=True,
            )
            raise typer.Exit(code=2)
        _write_to_dir(body, output_dir)
        return
    if output_format == OutputFormat.json:
        sys.stdout.write(json.dumps({'systemPromptAdditions': body}))
    else:
        sys.stdout.write(body)
    sys.stdout.flush()
