"""F43 — Resolution-flow primer pinning.

After 2026-05-14 compression: the 5-step resolution flow lives in the MCP
``memex_record_outcome`` and ``memex_memory_deprioritize`` tool descriptions
(authoritative source: ``memex_mcp._resolution_flow_descriptions``) and is
duplicated into the hermes-plugin tool schemas
(``RECORD_OUTCOME_SCHEMA``, ``MEMORY_DEPRIORITIZE_SCHEMA``). The hermes
briefing no longer carries it — see ``test_briefing.py`` for the briefing
contract.

This file pins:
- the flow keywords land in the MCP authoritative description
- the hermes-side tool schemas reference the flow + paired writes
- the verb-pair template fragment (``RESOLUTION_FLOW_PROMPT_FRAGMENT``) still
  carries the scaffolding for the verb-pair selection LLM call.
"""

from __future__ import annotations

import pytest

from memex_mcp._resolution_flow_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_RECORD_OUTCOME_DESCRIPTION,
)
from memex_hermes_plugin.memex.templates import (
    HISTORICAL_ROUTING_PROMPT_FRAGMENT,
    RESOLUTION_FLOW_PROMPT_FRAGMENT,
)


@pytest.mark.parametrize(
    'kw',
    [
        'Disambiguate',
        'memex_find_note',
        'Option A',
        'Option B',
        'Option C',
        '≥30',
        'memex_get_memory_units',
        'Mandatory LLM judgment',
        'memex_record_outcome',
        'memex_memory_deprioritize',
        'memex_get_unit_history',
        'apply_pre_filter=False',
        'evolved',
        'used to',
    ],
)
def test_mcp_record_outcome_description_includes_keyword(kw: str) -> None:
    """The MCP ``memex_record_outcome`` description is the authoritative carrier
    of the 5-step flow; pin each canonical keyword."""
    assert kw in MEMEX_RECORD_OUTCOME_DESCRIPTION, (
        f'MCP record_outcome description is missing keyword {kw!r}. '
        'See _resolution_flow_descriptions.py.'
    )


@pytest.mark.parametrize(
    'kw',
    [
        'Disambiguate',
        'memex_find_note',
        'Option A',
        'Option B',
        'Option C',
        '≥30',
        'memex_get_memory_units',
        'Mandatory LLM judgment',
        'memex_record_outcome',
        'memex_memory_deprioritize',
        'memex_get_unit_history',
        'apply_pre_filter=False',
    ],
)
def test_mcp_deprioritize_description_includes_keyword(kw: str) -> None:
    """The MCP ``memex_memory_deprioritize`` description also carries the 5-step
    flow because both verbs participate in it."""
    assert kw in MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION, (
        f'MCP deprioritize description is missing keyword {kw!r}. '
        'See _resolution_flow_descriptions.py.'
    )


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
    """The historical-routing fragment names F49 + apply_pre_filter=False."""
    assert 'memex_get_unit_history' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'apply_pre_filter=False' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'evolved' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'audit' in HISTORICAL_ROUTING_PROMPT_FRAGMENT


def test_hermes_record_outcome_schema_mentions_paired_writes() -> None:
    """The Hermes record_outcome tool description references the paired-write rule."""
    from memex_hermes_plugin.memex.tools import RECORD_OUTCOME_SCHEMA

    desc = RECORD_OUTCOME_SCHEMA['description']
    assert 'memex_memory_deprioritize' in desc, 'paired-write partner missing'
    assert '5-step flow' in desc or 'PAIRED writes' in desc, (
        'record_outcome schema should reference the 5-step paired-write flow'
    )


def test_hermes_deprioritize_schema_mentions_paired_writes() -> None:
    """The Hermes deprioritize tool description references the paired-write rule."""
    from memex_hermes_plugin.memex.tools import MEMORY_DEPRIORITIZE_SCHEMA

    desc = MEMORY_DEPRIORITIZE_SCHEMA['description']
    assert 'memex_record_outcome' in desc, 'paired-write partner missing'
    assert 'PAIRED writes' in desc or '5-step flow' in desc, (
        'deprioritize schema should reference the 5-step paired-write flow'
    )


def test_hermes_record_outcome_schema_carries_bare_success_invalid_warning() -> None:
    """The hermes record_outcome description must warn against bare success=True
    without a target. The server rejects that shape with 400; the description is
    the load-bearing surface that prevents the agent from emitting it."""
    from memex_hermes_plugin.memex.tools import RECORD_OUTCOME_SCHEMA

    desc = RECORD_OUTCOME_SCHEMA['description']
    assert 'INVALID' in desc and ('400' in desc or 'rejects' in desc), (
        'record_outcome description must warn that bare success=True is INVALID '
        'and the server rejects it.'
    )


def test_hermes_deprioritize_schema_carries_virtual_unit_warning() -> None:
    """The hermes deprioritize description must warn about virtual units (no DB row,
    deprio returns 404). This is the only surface that teaches the agent to filter
    candidates before pairing."""
    from memex_hermes_plugin.memex.tools import MEMORY_DEPRIORITIZE_SCHEMA

    desc = MEMORY_DEPRIORITIZE_SCHEMA['description']
    assert 'virtual' in desc.lower(), 'deprioritize description must teach virtual-unit handling'
    assert '404' in desc or 'no DB row' in desc, (
        'deprioritize description must explain why virtual units cannot be deprioritized'
    )
