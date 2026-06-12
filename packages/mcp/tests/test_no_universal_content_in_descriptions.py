"""Architecture-boundary fence — universal Tier 1b content must not leak into
MCP descriptions (Tier 1a).

The three-tier agent-surface architecture (post-2026-05-14) splits prose into:

- Tier 1a: MCP ``instructions=`` and per-tool ``description=``. Terse —
  what the tool does, required params, 4xx triggers, 1-sentence "when to
  use". No multi-step composition.
- Tier 1b: universal cross-agent doctrine in ``memex_common.agent_surface``.
  The 5-step resolution flow, Options A/B/C, retrieval-routing table by
  query type, KV scope-qualifier rule, virtual-unit warning, etc.

A regression where Tier 1b prose drifts back into Tier 1a costs tokens on
every tool-discovery turn and re-introduces the duplication this refactor
eliminated. This file pins the boundary by enforcing that banned phrases
(load-bearing markers of Tier 1b content) do NOT appear in any MCP tool
description nor in the MCP ``instructions=`` field.

If a banned phrase legitimately needs to appear in a tool description as
*contrast* (e.g. a description that contains "(see system prompt §...)"
naming the section), prefer naming the section without copying its content.
"""

from __future__ import annotations

import asyncio

import pytest

from memex_mcp.server import mcp


# Each entry: a phrase that should NEVER appear in MCP tool descriptions or
# the `instructions=` field. Matched case-sensitively (case captures the
# "Option A" verbatim of the prior MCP prose) unless explicitly downcased.
_BANNED_UNIVERSAL_PHRASES = (
    # 5-step flow scaffolding — belongs in agent_surface.RESOLUTION_FLOW.
    'Option A',
    'Option B',
    'Option C',
    'Mandatory LLM judgment',
    'Disambiguate',
    # Retrieval-routing fragment — belongs in agent_surface.RETRIEVAL_ROUTING.
    'apply_pre_filter=False',
    # 5-step flow keyword — belongs in agent_surface.RESOLUTION_FLOW.
    '5-step flow',
    'five-step flow',
    # Orthogonal-axes prose — belongs in agent_surface.AXES.
    'orthogonal axes',
    'MW gradient',
    # NOTE: "append-only MW counter" remains permissible in the deprioritize
    # description as a per-tool contrast — that's rationale-that-aids-
    # generalization for the agent picking record_outcome vs deprioritize.
    # We ban only the longer-prose AXES *section* content, not the
    # one-phrase contrast.
    # KV scope-qualifier-wins prose — belongs in agent_surface.KV_NAMESPACE.
    'scope qualifier wins',
    'narrower scope wins',
    'scope qualifier picks the namespace',
    # Historical/audit routing — belongs in agent_surface.HISTORICAL_ROUTING.
    # memex_get_unit_history naming itself is a tool name — agents that
    # name the verb (e.g. "see also memex_get_unit_history") are fine; we
    # only ban the *audit-routing prose phrase* that bundles it with the
    # apply_pre_filter=False bypass.
    'bypasses MW/FSFM/confidence filters',
)


async def _all_tool_descriptions() -> dict[str, str]:
    # Public FastMCP API — survives FastMCP upgrades.
    tools = await mcp.list_tools()
    return {t.name: (getattr(t, 'description', '') or '') for t in tools}


@pytest.fixture(scope='module')
def tool_descriptions() -> dict[str, str]:
    return asyncio.run(_all_tool_descriptions())


@pytest.mark.parametrize('phrase', _BANNED_UNIVERSAL_PHRASES)
def test_phrase_not_in_any_tool_description(phrase: str, tool_descriptions: dict[str, str]) -> None:
    """Universal-content phrases must not appear in any MCP tool description."""
    leaked = [name for name, desc in tool_descriptions.items() if phrase in desc]
    assert not leaked, (
        f'Tier 1b universal phrase {phrase!r} leaked into Tier 1a MCP tool '
        f'descriptions for tools: {leaked!r}. '
        'Move this content to `memex_common.agent_surface`; keep per-tool '
        'descriptions terse (contract + 4xx triggers + when-to-use only).'
    )


@pytest.mark.parametrize('phrase', _BANNED_UNIVERSAL_PHRASES)
def test_phrase_not_in_mcp_instructions(phrase: str) -> None:
    """Universal phrases must not appear in the MCP ``instructions=`` field."""
    text = getattr(mcp, 'instructions', None) or ''
    assert phrase not in text, (
        f'Tier 1b universal phrase {phrase!r} leaked into Tier 1a MCP '
        '`instructions=` field. Move this content to '
        '`memex_common.agent_surface` and reference it via the pointer line.'
    )


@pytest.mark.parametrize('phrase', _BANNED_UNIVERSAL_PHRASES)
def test_phrase_not_in_tier_2_harnesses(phrase: str) -> None:
    """Universal Tier 1b phrases must not leak into the Tier 2 harness
    strings either. The harnesses are agent-specific framing layered on
    top of ``compose_universal()`` — duplicating universal content there
    re-creates the drift class the SSOT eliminates."""
    from memex_common.agent_harnesses import CLAUDE_CODE_HARNESS, HERMES_HARNESS

    if phrase in HERMES_HARNESS:
        raise AssertionError(
            f'Universal phrase {phrase!r} leaked into HERMES_HARNESS. '
            'Tier 2 harnesses must not duplicate Tier 1b content '
            '(it already arrives via `compose_universal()`).'
        )
    if phrase in CLAUDE_CODE_HARNESS:
        raise AssertionError(
            f'Universal phrase {phrase!r} leaked into CLAUDE_CODE_HARNESS. '
            'Tier 2 harnesses must not duplicate Tier 1b content '
            '(it already arrives via `compose_universal()`).'
        )


def test_mcp_instructions_is_ssot_object() -> None:
    """``mcp.instructions`` MUST be the canonical
    ``memex_common.agent_surface.MCP_TRANSPORT_INSTRUCTIONS`` object —
    no local copy. The CLI ``agent-surface mcp`` target reads from the
    same SSOT, so both surfaces emit byte-identical bytes by construction."""
    from memex_common.agent_surface import MCP_TRANSPORT_INSTRUCTIONS

    assert mcp.instructions is MCP_TRANSPORT_INSTRUCTIONS, (
        '`mcp.instructions` is not the canonical '
        '`memex_common.agent_surface.MCP_TRANSPORT_INSTRUCTIONS` object — '
        'a local copy would let the MCP server and the CLI inspection '
        'surface drift apart.'
    )


def test_mcp_instructions_defers_doctrine_to_host_prompt() -> None:
    """``instructions=`` must tell composing agents that routing/storage/
    citation doctrine lives elsewhere (the host system prompt), not in the
    transport surface.

    The integrator-facing ``compose_universal()`` / ``compose_with_procedural()``
    composition recipe was removed in the register-rebalance: it was
    documentation for whoever builds the prompt, sitting in the field the
    *consuming* agent reads — the agent can't compose its own prompt, so the
    recipe was noise. We pin the replacement pointer line instead."""
    text = getattr(mcp, 'instructions', None) or ''
    assert 'host system prompt' in text, (
        'MCP `instructions=` should tell composing agents that routing, '
        'storage, and citation doctrine live in the host system prompt, '
        'not in the transport surface.'
    )
