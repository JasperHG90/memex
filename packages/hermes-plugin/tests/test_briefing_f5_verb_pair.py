"""F5 — summarize_node verb-pair invariant.

After 2026-05-14 compression: the briefing no longer carries the routing
guide. The summarize-node verb-pair contract (synchronous counterpart to
background reflect, rate-limit + retry_after_seconds envelope) now lives
in the MCP ``memex_memory_summarize_node`` tool description, which all
clients see at tool-discovery time.

Pure string-contains; the matching ``@pytest.mark.llm`` driven-agent verb
selection lives in ``tests/test_e2e_f5_llm_turn.py``.
"""

from __future__ import annotations

from memex_mcp._summarize_descriptions import MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION


def test_summarize_description_contrasts_against_background_reflect():
    """summarize_node and reflect must co-appear as a verb-pair."""
    text = MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION
    assert 'reflect' in text.lower()


def test_summarize_description_marks_verb_as_synchronous():
    """Per RFC-002, summarize_node is the SYNCHRONOUS counterpart to background reflect."""
    text = MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION.upper()
    assert 'SYNCHRONOUS' in text


def test_summarize_description_documents_rate_limit_and_retry_after():
    """Agent must know that retry_after_seconds is the structured back-off hint."""
    text = MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION.lower()
    assert 'rate-limit' in text or 'rate limit' in text or 'rate_limit' in text
    assert 'retry_after_seconds' in text
