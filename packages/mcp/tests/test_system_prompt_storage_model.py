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


def _storage_model_section(text: str) -> str:
    """Return the STORAGE MODEL paragraph block.

    Scoping invariant assertions to this block (not the whole instructions)
    means common words like ``edit`` / ``replace`` / ``delete`` legitimately
    appearing elsewhere in the prompt cannot satisfy the check.
    """
    # Guard the slice so a renamed/removed marker fails with a clear message
    # rather than an opaque ValueError from .index().
    assert 'STORAGE MODEL' in text, "'STORAGE MODEL' marker missing from instructions"
    start = text.index('STORAGE MODEL')
    assert 'ROUTING' in text[start:], "'ROUTING' marker missing after STORAGE MODEL"
    # The block ends at the next blank line followed by a top-level marker
    # (``RULE:``, ``ROUTING``, etc.). Take a generous window — the section is
    # ~20 lines.
    end = text.index('ROUTING', start)
    return text[start:end]


def test_instructions_contain_storage_model_primer():
    text = _instructions()
    assert 'STORAGE MODEL' in text


def test_storage_model_names_three_layers():
    text = _instructions()
    section = _storage_model_section(text)
    assert '**Notes**' in section
    assert '**Memory units**' in section
    assert '**KV store**' in section


def test_storage_model_states_append_only_invariant():
    text = _instructions()
    section = _storage_model_section(text)
    assert 'Append-only' in section or 'append-only' in section
    # The verbs must appear inside an explicit prohibition. A revert that
    # turns the bullet permissive (e.g. "you can edit, replace, or delete...")
    # would still satisfy a bare "verb in section" check yet leak the
    # OrangeHermes regression — so require a negation phrase too.
    negation_phrases = (
        'Do NOT try to edit, replace, or delete',
        "Don't try to edit, replace, or delete",
        'Do not try to edit, replace, or delete',
    )
    assert any(p in section for p in negation_phrases), (
        f'STORAGE MODEL must contain an explicit prohibition like {negation_phrases[0]!r}'
    )
    # Explicitly forbid edit/replace/delete on memory units. Scope to the
    # storage-model section so unrelated routing bullets ("Do NOT attempt
    # to delete KV entries", etc.) cannot satisfy the check on their own.
    for forbidden_action in ('edit', 'replace', 'delete'):
        assert forbidden_action in section, (
            f'{forbidden_action!r} missing from STORAGE MODEL section'
        )


def test_storage_model_describes_reflection_as_read_only():
    """The storage-model section must teach that reflection's output is read-only."""
    text = _instructions()
    section = _storage_model_section(text)
    assert 'reflection' in section.lower()
    assert 'observations' in section.lower()
    assert 'read-only' in section


def test_kv_routing_no_longer_calls_kv_a_fact_store():
    """The IF/THEN routing block must not advertise KV as a fact store.

    Regression fence for the OrangeHermes muddle: agents wrote system-status
    flags into KV to mark stale memory units 'resolved' because the prompt
    said 'storing/retrieving structured facts ... → KV STORE'.
    """
    text = _instructions()
    # Scope to the KV routing block (the IF/THEN bullet) so a contrastive
    # mention elsewhere can't satisfy the assertions.
    kv_idx = text.index('→ KV STORE')
    kv_block = text[kv_idx : kv_idx + 1200]
    assert 'storing/retrieving structured facts' not in kv_block
    assert 'store a user fact or preference' not in kv_block
    assert 'fuzzy semantic search over stored facts' not in kv_block
    assert 'list all stored facts' not in kv_block


def test_entity_mentions_routing_uses_memory_units_term():
    """Entity-mentions routing line must say 'memory units', not bare 'facts'."""
    text = _instructions()
    em_idx = text.index('memex_get_entity_mentions(entity_id)')
    em_line_end = text.index('\n', em_idx)
    em_line = text[em_idx:em_line_end]
    assert 'memory units' in em_line
