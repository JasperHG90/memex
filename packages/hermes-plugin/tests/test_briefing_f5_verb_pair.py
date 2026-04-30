"""F5 — Hermes briefing verb-pair invariant (TC6a).

Asserts the Hermes routing guide:
1. Mentions ``memex_memory_summarize_node`` as the SYNCHRONOUS verb.
2. Contrasts it with background ``reflect`` (the queued/scheduler-driven path).
3. Documents the rate limit and ``retry_after_seconds`` envelope so the agent
   does not retry-loop.

Pure string-contains; the matching ``@pytest.mark.llm`` driven-agent verb
selection lives in ``tests/test_e2e_f5_llm_turn.py``.
"""

from __future__ import annotations

from memex_hermes_plugin.memex.briefing import _ROUTING_GUIDE


def test_routing_guide_mentions_summarize_node_verb():
    assert 'memex_memory_summarize_node' in _ROUTING_GUIDE


def test_routing_guide_contrasts_summarize_node_against_reflect():
    """summarize_node and reflect must appear in the same routing block as a verb-pair."""
    text = _ROUTING_GUIDE
    assert 'memex_memory_summarize_node' in text
    assert 'reflect' in text.lower()


def test_routing_guide_marks_summarize_node_as_synchronous():
    """Per RFC-002, summarize_node is the SYNCHRONOUS counterpart to background reflect."""
    text = _ROUTING_GUIDE.upper()
    assert 'SYNCHRONOUS' in text


def test_routing_guide_documents_rate_limit_and_retry_after():
    """Agent must know that retry_after_seconds is the structured back-off hint."""
    text = _ROUTING_GUIDE.lower()
    assert 'rate-limit' in text or 'rate limit' in text or 'rate_limit' in text
    assert 'retry_after_seconds' in text
