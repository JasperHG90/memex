"""Regression fences for the MCP server's system-prompt storage-model primer.

These tests guard against the OrangeHermes-class bug from regressing in MCP
prose: agents conflated KV writes with memory-unit updates because the prose
overloaded the word ``fact`` across both layers and never taught the
append-only / supersession invariant.

The tests assert structural invariants of ``mcp.instructions`` (the system
prompt the model sees), not exact wording, so prose can evolve without
causing churn — but the muddle cannot return.
"""

from __future__ import annotations

from memex_mcp.server import mcp


def _instructions() -> str:
    """Pull the system prompt off the FastMCP instance.

    FastMCP exposes the ``instructions`` constructor arg as a public attribute.
    """
    text = getattr(mcp, 'instructions', None)
    assert isinstance(text, str) and text, 'mcp.instructions missing or empty'
    return text


def test_instructions_contain_storage_model_primer():
    text = _instructions()
    assert 'STORAGE MODEL' in text


def test_storage_model_names_three_layers():
    text = _instructions()
    assert '**Notes**' in text
    assert '**Memory units**' in text
    assert '**KV store**' in text


def test_storage_model_states_append_only_invariant():
    text = _instructions()
    assert 'Append-only' in text or 'append-only' in text
    # Explicitly forbid edit/replace/delete on memory units.
    for forbidden_action in ('edit', 'replace', 'delete'):
        assert forbidden_action in text


def test_storage_model_warns_against_manual_supersession():
    text = _instructions()
    assert 'never mark memory units stale' in text or 'never mark' in text


def test_kv_routing_no_longer_calls_kv_a_fact_store():
    """The IF/THEN routing block must not advertise KV as a fact store.

    Regression fence for the OrangeHermes muddle: agents wrote system-status
    flags into KV to mark stale memory units 'resolved' because the prompt
    said 'storing/retrieving structured facts ... → KV STORE'.
    """
    text = _instructions()
    # KV bullet must not invite agents to store *learned* facts in KV.
    assert 'storing/retrieving structured facts' not in text
    assert 'store a user fact or preference' not in text
    assert 'fuzzy semantic search over stored facts' not in text
    assert 'list all stored facts' not in text


def test_kv_routing_redirects_learned_facts_to_notes():
    """The KV routing block must explicitly route learned content to notes."""
    text = _instructions()
    # Find the KV STORE block, scope assertions to it.
    kv_idx = text.index('→ KV STORE')
    # Look at the next ~600 chars (covers the full bullet block).
    kv_block = text[kv_idx : kv_idx + 800]
    assert 'NOT for facts learned from content' in kv_block
    assert 'memex_add_note' in kv_block


def test_entity_mentions_routing_uses_memory_units_term():
    """Entity-mentions routing line must say 'memory units', not bare 'facts'."""
    text = _instructions()
    em_idx = text.index('memex_get_entity_mentions(entity_id)')
    em_line_end = text.index('\n', em_idx)
    em_line = text[em_idx:em_line_end]
    assert 'memory units' in em_line
