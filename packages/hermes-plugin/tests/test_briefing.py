"""Tests for briefing cache + block formatting.

After 2026-05-14 compression: the briefing no longer carries the storage
model, retrieval routing guide, or 5-step resolution flow — those moved to
the MCP server ``instructions`` field and per-tool descriptions. The
briefing now carries only the agent-side nudges (citation discipline,
KV scope-qualifier rule, outcome verb routing, capture cadence) plus the
layer-routing primer table.

OrangeHermes regression fences that used to live here have been migrated
to ``packages/mcp/tests/test_mcp_instructions_regression.py`` (storage
model append-only invariant) and ``test_kv_write_description.py`` (KV
"not for facts learned from content" rule).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from memex_hermes_plugin.memex.briefing import (
    BriefingCache,
    _AGENT_NUDGE,
    _CITATION_DISCIPLINE,
    format_briefing_block,
)


def test_cache_returns_empty_on_timeout():
    cache = BriefingCache()
    api = Mock()

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return 'late'

    api.get_session_briefing = slow
    cache.start_fetch(api, vault_id=uuid4(), budget=2000, project_id='p')
    assert cache.get(timeout=0.1) == ''


def test_cache_returns_result():
    cache = BriefingCache()
    api = Mock()
    api.get_session_briefing = AsyncMock(return_value='# Briefing\nRecent work: X.')
    cache.start_fetch(api, vault_id=uuid4(), budget=2000, project_id='p')
    assert 'Briefing' in cache.get(timeout=5.0)


def test_cache_records_error():
    cache = BriefingCache()
    api = Mock()
    api.get_session_briefing = AsyncMock(side_effect=RuntimeError('boom'))
    cache.start_fetch(api, vault_id=uuid4(), budget=2000, project_id='p')
    cache.get(timeout=5.0)
    assert 'boom' in (cache.get_error() or '')


def test_cache_reset_clears_state():
    cache = BriefingCache()
    api = Mock()
    api.get_session_briefing = AsyncMock(return_value='hello')
    cache.start_fetch(api, vault_id=uuid4(), budget=2000, project_id='p')
    cache.get(timeout=5.0)
    cache.reset()
    assert cache.get(timeout=0.01) == ''


def test_format_block_with_vault_and_briefing():
    block = format_briefing_block(
        '# Recent activity\n- Did X',
        vault_id='my-vault',
        project_id='github.com/acme/x',
        session_note_key='hermes:session:2026-01-01T00:00:00.000Z',
        kv_instructions_if_no_vault=False,
    )
    assert 'Memex Memory' in block
    assert '`my-vault`' in block
    assert 'github.com/acme/x' in block
    assert 'hermes:session:2026-01-01T00:00:00.000Z' in block
    assert '# Recent activity' in block


def test_format_block_carries_agent_nudge():
    """The agent-side nudges (citations, KV scope rule, outcome verbs, capture)
    are the briefing's payload after compression."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    assert 'Working with Memex (agent-side reminders)' in block
    assert 'Citing sources' in block


def test_format_block_carries_layer_routing_primer():
    """The 4-layer routing table is the only big primer that stays in the briefing
    (canonical source is ``memex_common.agent_surface.LAYER_ROUTING_PRIMER_TABLE``)."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    assert 'Memory layers and tool routing' in block


def test_format_block_without_vault_adds_kv_guidance():
    block = format_briefing_block(
        '',
        vault_id=None,
        project_id='p',
        session_note_key='hermes:session:abc',
        kv_instructions_if_no_vault=True,
    )
    assert 'No vault bound' in block
    assert 'project:p:vault' in block


def test_format_block_skips_briefing_section_when_empty():
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    # The `\n---\n` literal is the briefing-section separator added in
    # format_briefing_block only when the `briefing` argument is non-empty.
    assert '\n---\n' not in block


# --- Compression invariants: things that should NO LONGER be in the briefing ---


def test_briefing_does_not_carry_storage_model_primer():
    """Storage model moved to MCP server `instructions` field — no duplicate here."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    # The old heading is gone. Storage-model facts live in MCP server instructions.
    assert '### How Memex stores knowledge' not in block


def test_briefing_does_not_carry_routing_guide():
    """Retrieval routing moved to MCP server `instructions` field — no duplicate here."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    assert '### How to use Memex tools' not in block


def test_briefing_does_not_carry_resolution_flow_primer():
    """5-step resolution flow lives in MCP `memex_record_outcome` /
    `memex_memory_deprioritize` tool descriptions — no duplicate here."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    # The flow's step-by-step prose is now in the tool descriptions, not here.
    # The briefing only carries a one-line nudge pointing at those descriptions.
    assert 'Disambiguate first' not in block
    assert 'Step 1 — Disambiguate' not in block


# --- Agent-side nudges (the briefing's actual payload) ---


def test_citation_discipline_in_briefing():
    """Citations are an agent-side output property, not a tool contract; stays in briefing."""
    assert 'Citing sources' in _CITATION_DISCIPLINE
    assert 'square brackets' in _CITATION_DISCIPLINE
    assert 'Do not fabricate' in _CITATION_DISCIPLINE


def test_agent_nudge_carries_scope_qualifier_rule():
    """KV namespace nudge: scope qualifier (not grammatical person) picks the namespace.
    This is the disambiguation rule that the hermes agent uses to route 'my preference
    for this project' to `project:` instead of `user:`."""
    assert 'scope qualifier' in _AGENT_NUDGE
    # The four namespaces all show up as routing targets.
    for ns in ('user:', 'project:', 'global:', 'app:'):
        assert ns in _AGENT_NUDGE
    # The disambiguating example.
    assert 'this project' in _AGENT_NUDGE


def test_agent_nudge_carries_outcome_verb_routing():
    """Hermes-specific nudge: map user signals to record_outcome verbs.
    The tool description carries the contract; this nudges the agent to recognize
    the signal phrases."""
    assert 'success verb' in _AGENT_NUDGE.lower() or 'success signal' in _AGENT_NUDGE.lower()
    assert 'memex_record_outcome' in _AGENT_NUDGE
    assert 'memex_memory_deprioritize' in _AGENT_NUDGE


def test_agent_nudge_carries_capture_cadence():
    """The capture nudge tells the agent when to write a note proactively.
    Token budget cap is the load-bearing assertion (resists drift toward verbose notes)."""
    assert 'memex_add_note' in _AGENT_NUDGE
    assert '300 tokens' in _AGENT_NUDGE
