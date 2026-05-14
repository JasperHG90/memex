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
import sys
from enum import Enum

import typer

from memex_common.agent_harnesses import CLAUDE_CODE_HARNESS, HERMES_HARNESS
from memex_common.agent_surface import compose_universal


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


_MCP_TRANSPORT = """## Memex MCP — transport facts

Progressive disclosure: `memex_tags()` → `memex_search(query, tags=[...])` → `memex_get_schema(tools=[...])` → call by name.

Vault defaults: writes default to the active vault; reads default to search vaults (.memex.yaml / global config). Pass `vault_id`/`vault_ids` to override.

System-prompt composition: load `memex_common.agent_surface.compose_universal()` (Python) or `memex agent-surface universal` (shell) for the universal Tier 1b system-prompt content. This MCP `instructions=` field is intentionally minimal — it carries transport facts only."""


def _compose_for_target(target: Target) -> str:
    if target in (Target.universal, Target.generic):
        return compose_universal()
    if target == Target.hermes:
        return compose_universal() + '\n\n' + HERMES_HARNESS
    if target == Target.claude_code:
        return compose_universal() + '\n\n' + CLAUDE_CODE_HARNESS
    if target == Target.mcp:
        return _MCP_TRANSPORT
    raise ValueError(f'unknown target: {target!r}')


# `app.command()` with `no_args_is_help=True` on the single-command app makes
# typer flatten this into `memex agent-surface TARGET [OPTIONS]`.
@app.command()
def emit(
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
) -> None:
    """Emit the system-prompt content for the requested target to stdout."""
    body = _compose_for_target(target)
    if output_format == OutputFormat.json:
        sys.stdout.write(json.dumps({'systemPromptAdditions': body}))
    else:
        sys.stdout.write(body)
    sys.stdout.flush()
