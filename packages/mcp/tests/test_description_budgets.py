"""Token-budget enforcement for MCP Tier 1a surfaces (post-2026-05-14).

The three-tier agent-surface architecture caps each tier at a load-bearing
size so context budgets stay predictable across long sessions (context
degrades at 80K tokens; severe at 180K per indiehackers' Claude Code
reverse-engineering analysis). MCP descriptions are Tier 1a:

- ``instructions=`` field: ≤2,000 chars / ≤500 tokens. Recomputed each turn
  (dbreunig — MCP `instructions=` is on the dynamic side of the cache
  boundary), so it must stay small.
- Per-tool description: ≤1,200 chars / ≤300 tokens. Tool contract + 4xx
  triggers + 1-sentence "when to use". NO multi-step composition (that's
  Tier 1b in ``agent_surface``).

Char/token ratio: empirical measurement against ``tiktoken cl100k_base``
on this repo's agent_surface markdown content yields **3.5 chars/token**
(memex_common.agent_surface.compose_universal() = 4,934 chars / 1,318
tokens → 3.74 cpt; round down to 3.5 for headroom). Char budget is
multiplied by 3.5 cpt to get the token approximation; the char check is
the load-bearing assertion (chars are deterministic; tokens depend on
the model's BPE).

If a description legitimately needs more space (e.g., a tool with many
required params and example shapes), bump the cap with intention — the
test failure is the conversation, not a rubber-stamp.
"""

from __future__ import annotations

import asyncio

import pytest

from memex_mcp.server import mcp


_INSTRUCTIONS_CHAR_CAP = 2_000
_TOOL_DESC_CHAR_CAP = 1_200

# F3 deliberate choice: five search tools embed
# ``memex_common.agent_surface.LAYER_ROUTING_PRIMER_FRAGMENT`` so an agent
# picking among them has the routing decision inline with the tool. This
# adds ~600 chars to each description; the cap is bumped per-tool below.
# The primer itself is still authored once (SSOT) — the inclusion is a
# per-tool strategic-guidance choice, not duplication.
_LAYER_PRIMER_TOOLS_CHAR_CAP = 1_800
_LAYER_PRIMER_TOOLS = frozenset(
    {
        'memex_memory_search',
        'memex_note_search',
        'memex_survey',
        'memex_get_entity_mentions',
        'memex_kv_search',
    }
)


def _approx_tokens(text: str) -> int:
    """Approximate token count at ~3.5 chars/token (empirically measured
    against ``tiktoken cl100k_base`` on this repo's agent_surface markdown).
    Multiply chars by 2 then divide by 7 → same ratio, integer-only arithmetic."""
    return (len(text) * 2 + 6) // 7


def test_mcp_instructions_within_budget() -> None:
    """MCP ``instructions=`` is Tier 1a transport-only — must stay terse."""
    text = getattr(mcp, 'instructions', None) or ''
    assert text, 'mcp.instructions is empty'
    assert len(text) <= _INSTRUCTIONS_CHAR_CAP, (
        f'mcp.instructions is {len(text)} chars (~{_approx_tokens(text)} tok); '
        f'cap is {_INSTRUCTIONS_CHAR_CAP} chars. Move universal content to '
        '`memex_common.agent_surface`.'
    )


async def _all_tool_descriptions() -> dict[str, str]:
    # ``list_tools()`` is the public FastMCP API and returns the same
    # FunctionTool objects as the private ``_list_tools`` — use the public
    # form so a future FastMCP release doesn't silently break this fence.
    tools = await mcp.list_tools()
    return {t.name: (getattr(t, 'description', '') or '') for t in tools}


@pytest.fixture(scope='module')
def tool_descriptions() -> dict[str, str]:
    return asyncio.run(_all_tool_descriptions())


def test_every_tool_has_description(tool_descriptions: dict[str, str]) -> None:
    """Every registered tool carries a non-empty description."""
    missing = [name for name, desc in tool_descriptions.items() if not desc.strip()]
    assert not missing, f'tools with empty description: {missing!r}'


def _cap_for(name: str) -> int:
    return _LAYER_PRIMER_TOOLS_CHAR_CAP if name in _LAYER_PRIMER_TOOLS else _TOOL_DESC_CHAR_CAP


def test_layer_primer_only_in_F3_tools(tool_descriptions: dict[str, str]) -> None:
    """The 4-layer routing primer is intentionally embedded inline in
    exactly the 5 F3 search tools (per the F3 design). It must NOT appear
    in any OTHER tool description — that would silently double the
    Tier 1a budget for that tool and undo the consolidation."""
    # Unique sentinel from LAYER_ROUTING_PRIMER_FRAGMENT that wouldn't appear
    # anywhere else in a tool contract.
    primer_marker = 'Memex memory layers (route by query type):'
    leaked = [
        name
        for name, desc in tool_descriptions.items()
        if primer_marker in desc and name not in _LAYER_PRIMER_TOOLS
    ]
    assert not leaked, (
        f'LAYER_ROUTING_PRIMER_FRAGMENT leaked into non-F3 tools: {leaked!r}. '
        f'The primer is only meant to inline into {sorted(_LAYER_PRIMER_TOOLS)!r}.'
    )


def test_each_tool_description_within_budget(tool_descriptions: dict[str, str]) -> None:
    """Per-tool descriptions stay within the Tier 1a cap (F3 primer tools
    have a higher cap because they embed ``LAYER_ROUTING_PRIMER_FRAGMENT``)."""
    over = {
        name: (len(desc), _approx_tokens(desc), _cap_for(name))
        for name, desc in tool_descriptions.items()
        if len(desc) > _cap_for(name)
    }
    assert not over, (
        'tool descriptions exceed their cap: '
        f'{ {k: f"{c} chars (~{t} tok); cap={cap}" for k, (c, t, cap) in over.items()} }. '
        'Move multi-step / universal content to `memex_common.agent_surface` '
        'and keep the per-tool description to contract + 4xx + when-to-use.'
    )
