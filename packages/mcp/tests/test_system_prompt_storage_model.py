"""Regression fences for the storage-model primer — relocated to agent_surface.

Before 2026-05-14: this content lived in the MCP `instructions=` field
and these tests pinned it there. The OrangeHermes regression fence
(agents conflated KV writes with memory-unit updates) was anchored to
the prose in `server.py`.

After 2026-05-14 (three-tier agent-surface architecture): universal
content moved to ``memex_common.agent_surface`` (Tier 1b). MCP
`instructions=` carries only transport-level facts (Tier 1a). These
tests follow the content to its new home and additionally fence the
NEW architecture boundary (no universal content leaks back into MCP).
"""

from __future__ import annotations

from memex_common.agent_surface import STORAGE_MODEL, compose_universal
from memex_mcp.server import mcp


def _mcp_instructions() -> str:
    text = getattr(mcp, 'instructions', None)
    assert isinstance(text, str) and text, 'mcp.instructions missing or empty'
    return text


# ---------------------------------------------------------------------------
# Storage-model regression fences (relocated to agent_surface).
# ---------------------------------------------------------------------------


def test_storage_model_section_exists_in_agent_surface():
    assert STORAGE_MODEL


def test_storage_model_names_three_layers():
    assert '**Notes**' in STORAGE_MODEL
    assert '**Memory units**' in STORAGE_MODEL
    assert '**KV store**' in STORAGE_MODEL


def test_storage_model_states_append_only_invariant():
    """Append-only invariant must include an explicit prohibition (do/don't
    edit/replace/delete). A permissive bullet would still satisfy a bare
    keyword check yet leak the regression — require the negation phrase."""
    assert 'append-only' in STORAGE_MODEL.lower()
    negation_phrases = (
        'NEVER edit/replace/delete',
        'Do NOT edit, replace, or delete',
        "Don't edit, replace, or delete",
        'Do not edit, replace, or delete',
    )
    assert any(p in STORAGE_MODEL for p in negation_phrases), (
        f'STORAGE_MODEL must contain an explicit prohibition like '
        f'{negation_phrases[0]!r}; got: {STORAGE_MODEL!r}'
    )


def test_storage_model_describes_reflection_as_read_only():
    assert 'reflection' in STORAGE_MODEL.lower()
    assert 'mental model' in STORAGE_MODEL.lower()
    assert 'read-only' in STORAGE_MODEL


def test_kv_layer_described_in_storage_model():
    """KV is the third storage layer; must be present in the model overview.

    The KV-vs-content-fact distinction (the OrangeHermes regression fence)
    is now enforced at the per-tool description layer; see
    ``packages/common/tests/test_tool_descriptions.py
    ::test_kv_put_rejects_content_facts``.
    """
    assert '**KV store**' in STORAGE_MODEL


# ---------------------------------------------------------------------------
# Architecture boundary — universal content must NOT leak back into MCP
# instructions.
# ---------------------------------------------------------------------------


def test_mcp_instructions_does_not_carry_storage_model():
    """MCP instructions is Tier 1a (transport-only). Storage-model prose
    must NOT reappear there. If a future PR adds it back, that's drift."""
    text = _mcp_instructions()
    assert 'STORAGE MODEL' not in text, (
        'Storage-model content moved to memex_common.agent_surface. '
        'Do not re-introduce it in the MCP `instructions=` field.'
    )
    assert '**Memory units**' not in text


def test_mcp_instructions_defer_doctrine_to_host_prompt():
    """The terse Tier-1a MCP transport instructions must NOT inline the
    routing/storage/citation doctrine — that lives in the Tier-1b agent
    surface injected into the HOST system prompt. (Naming the Python SSOT
    module path to a runtime LLM would be meaningless; deferring to the host
    prompt is the contract.)"""
    text = _mcp_instructions()
    assert 'host system prompt' in text.lower()


# ---------------------------------------------------------------------------
# Composer pulls STORAGE_MODEL through to the universal block.
# ---------------------------------------------------------------------------


def test_compose_universal_contains_storage_model():
    out = compose_universal()
    assert '**Notes**' in out
    assert '**Memory units**' in out
    assert '**KV store**' in out
