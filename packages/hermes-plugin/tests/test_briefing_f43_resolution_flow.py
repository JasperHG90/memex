"""Resolution-flow primer pinning — post-2026-05-14 three-tier architecture.

Before 2026-05-14: the 5-step resolution flow lived verbatim in the MCP
``memex_record_outcome`` and ``memex_memory_deprioritize`` tool descriptions
(and was duplicated into hermes-plugin tool schemas).

After 2026-05-14 (three-tier agent-surface architecture):
- Universal content (the 5-step flow, Options A/B/C, `top_k>=30`, paired-write
  routing) lives in ``memex_common.agent_surface.RESOLUTION_FLOW``,
  ``HISTORICAL_ROUTING``, ``CRITICAL_HEADER``, and ``CRITICAL_FOOTER`` —
  surfaced by ``compose_universal()`` (Tier 1b).
- MCP tool descriptions (Tier 1a) carry ONLY the per-tool contract:
  what the verb does, required params, 4xx triggers, paired-write partner.
  They MUST NOT carry the multi-step composition flow.
- Hermes schemas import the same tool descriptions from
  ``memex_common.tool_descriptions`` — no per-package drift possible.

This file pins both halves of the new contract:
1. ``compose_universal()`` carries every flow concept.
2. MCP descriptions stay terse — universal-flow phrases must NOT reappear there.
3. Hermes schemas import the tool description from common (identity check).

The verb-pair scaffolding LLM template fragment (``RESOLUTION_FLOW_PROMPT_FRAGMENT``)
is a separate per-call surface and still carries the flow scaffolding.
"""

from __future__ import annotations

import pytest

from memex_common.agent_surface import compose_universal
from memex_common.tool_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESC,
    MEMEX_RECORD_OUTCOME_DESC,
)
from memex_hermes_plugin.memex.templates import (
    HISTORICAL_ROUTING_PROMPT_FRAGMENT,
    RESOLUTION_FLOW_PROMPT_FRAGMENT,
)


# ---------------------------------------------------------------------------
# Tier 1b universal-block parity — the SSOT for the flow.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'kw',
    [
        'Disambiguate',
        'memex_find_note',
        'A entity-anchored',
        'B cross-note',
        'C single-note',
        '≥30',
        'memex_get_memory_units',
        'memex_record_outcome',
        'memex_memory_deprioritize',
        'memex_get_unit_history',
        'apply_pre_filter=False',
        'evolved',
        'used to',
    ],
)
def test_universal_block_includes_resolution_flow_keyword(kw: str) -> None:
    """The ``agent_surface.compose_universal()`` output is the SSOT for the
    5-step flow; pin each canonical keyword there."""
    text = compose_universal()
    assert kw in text, (
        f'Universal block (compose_universal()) is missing keyword {kw!r}. '
        'Universal content lives in memex_common.agent_surface — '
        'RESOLUTION_FLOW + HISTORICAL_ROUTING + AXES.'
    )


# ---------------------------------------------------------------------------
# Tier 1a MCP boundary — descriptions must NOT carry the multi-step flow.
# ---------------------------------------------------------------------------


_BANNED_FROM_TIER_1A = (
    # 5-step flow scaffolding belongs in agent_surface, not in tool descriptions.
    'Disambiguate',
    'Option A',
    'Option B',
    'Option C',
    'Mandatory LLM judgment',
    # apply_pre_filter is a retrieval-routing concept, not a tool contract.
    'apply_pre_filter=False',
)


@pytest.mark.parametrize('phrase', _BANNED_FROM_TIER_1A)
def test_mcp_record_outcome_description_does_not_carry_universal_flow(phrase: str) -> None:
    """MCP record_outcome description (Tier 1a) must NOT carry the universal
    multi-step composition flow — that's Tier 1b content (agent_surface)."""
    assert phrase not in MEMEX_RECORD_OUTCOME_DESC, (
        f'Tier 1a record_outcome description leaks Tier 1b content {phrase!r}. '
        'Move it to memex_common.agent_surface.'
    )


@pytest.mark.parametrize('phrase', _BANNED_FROM_TIER_1A)
def test_mcp_deprioritize_description_does_not_carry_universal_flow(phrase: str) -> None:
    """MCP deprioritize description (Tier 1a) must NOT carry the universal
    multi-step composition flow — that's Tier 1b content (agent_surface)."""
    assert phrase not in MEMEX_MEMORY_DEPRIORITIZE_DESC, (
        f'Tier 1a deprioritize description leaks Tier 1b content {phrase!r}. '
        'Move it to memex_common.agent_surface.'
    )


# ---------------------------------------------------------------------------
# Per-tool contracts (Tier 1a) — what stays in the description.
# ---------------------------------------------------------------------------


def test_record_outcome_description_carries_v11_contract() -> None:
    """The V11 paired-write shape must be in the per-tool contract (Tier 1a).
    The partner verb (`memex_memory_deprioritize`) is universal-flow content
    and lives in agent_surface, not in this terse per-tool description."""
    assert 'units=[' in MEMEX_RECORD_OUTCOME_DESC
    assert 'verb' in MEMEX_RECORD_OUTCOME_DESC
    assert 'HTTP 400' in MEMEX_RECORD_OUTCOME_DESC


def test_deprioritize_description_carries_partner_and_virtual_unit() -> None:
    """Deprioritize (Tier 1a) carries: partner verb, virtual-unit 404 trigger."""
    assert 'memex_record_outcome' in MEMEX_MEMORY_DEPRIORITIZE_DESC
    assert 'virtual' in MEMEX_MEMORY_DEPRIORITIZE_DESC.lower()
    assert '404' in MEMEX_MEMORY_DEPRIORITIZE_DESC


# ---------------------------------------------------------------------------
# Tier 2 hermes schemas re-export Tier 1a descriptions by identity (no drift).
# ---------------------------------------------------------------------------


def test_hermes_record_outcome_schema_reexports_tier_1a_description() -> None:
    """The hermes RECORD_OUTCOME_SCHEMA description must be the
    ``memex_common.tool_descriptions.MEMEX_RECORD_OUTCOME_DESC`` object
    (identity, not just equality) — no separate hermes-side copy."""
    from memex_hermes_plugin.memex.tools import RECORD_OUTCOME_SCHEMA

    assert RECORD_OUTCOME_SCHEMA['description'] is MEMEX_RECORD_OUTCOME_DESC, (
        'Hermes record_outcome description drifted from common SSOT. '
        'Import from memex_common.tool_descriptions.'
    )


def test_hermes_deprioritize_schema_reexports_tier_1a_description() -> None:
    """The hermes MEMORY_DEPRIORITIZE_SCHEMA description must be the
    common ``MEMEX_MEMORY_DEPRIORITIZE_DESC`` object (identity check)."""
    from memex_hermes_plugin.memex.tools import MEMORY_DEPRIORITIZE_SCHEMA

    assert MEMORY_DEPRIORITIZE_SCHEMA['description'] is MEMEX_MEMORY_DEPRIORITIZE_DESC, (
        'Hermes deprioritize description drifted from common SSOT. '
        'Import from memex_common.tool_descriptions.'
    )


# ---------------------------------------------------------------------------
# Verb-pair scaffolding (per-call LLM template, separate from system prompt).
# ---------------------------------------------------------------------------


def test_resolution_flow_template_fragment_present() -> None:
    """The verb-pair scaffolding template fragment carries the 5-step flow.
    This fragment drives the verb-pair-selection LLM call inside Hermes; it is
    a separate surface from the tool descriptions."""
    assert 'DISAMBIGUATE' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'ROUTE' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'JUDGE' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'RECORD' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'DEPRIORITIZE' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'top_k>=30' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'memex_record_outcome' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'memex_memory_deprioritize' in RESOLUTION_FLOW_PROMPT_FRAGMENT


def test_historical_routing_template_fragment_present() -> None:
    """The historical-routing fragment names the audit-bypass invariant."""
    assert 'memex_get_unit_history' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'apply_pre_filter=False' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'evolved' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'audit' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
