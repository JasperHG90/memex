"""F5 — Real-LLM-turn validation (TC7, AC-F5-5).

Drives a real LLM via dspy to test that, given the F5 verb description,
the agent correctly picks ``memex_memory_summarize_node`` (synchronous)
versus background ``reflect`` (queued) when faced with a mid-conversation
contradiction signal.

Two cases:
1. Explicit-request flow: user says "consolidate what you know about
   X right now" → agent picks summarize_node with scope='full'.
2. Conflict-signal flow: user surfaces conflicting facts about an entity
   → agent picks summarize_node (incremental is acceptable).

GOOGLE_API_KEY skip is in the test body, NOT the decorator (canonical
Memex LLM-test pattern — see test_int_contradiction.py:212).
"""

from __future__ import annotations

import os

import pytest

from memex_mcp._summarize_descriptions import MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION


def _build_predictor():
    import dspy

    api_key = os.environ['GOOGLE_API_KEY']
    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key, timeout=120)

    _signature_doc = (
        'Decide which Memex reflection verb to call.\n\n'
        'Tool surface available:\n'
        '- memex_memory_summarize_node(entity_id, scope): SYNCHRONOUS reflection.\n'
        '  Description: ' + MEMEX_MEMORY_SUMMARIZE_NODE_DESCRIPTION + '\n'
        '- background reflect: queued, scheduler-driven; runs eventually but does '
        'NOT return a result this turn.\n\n'
        'Output the bare verb name only (one of: memex_memory_summarize_node, '
        'background_reflect) — no prose.'
    )

    class VerbSelection(dspy.Signature):
        __doc__ = _signature_doc

        user_signal: str = dspy.InputField(
            desc='Description of what the user said and the agent state.'
        )
        verb: str = dspy.OutputField(
            desc='Which verb to call: memex_memory_summarize_node OR background_reflect.'
        )

    return dspy.Predict(VerbSelection), lm


@pytest.mark.integration
@pytest.mark.llm
def test_real_agent_picks_summarize_node_on_explicit_request():
    """TC7a: user explicitly asks for in-session consolidation."""
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    import dspy

    predictor, lm = _build_predictor()

    with dspy.context(lm=lm):
        response = predictor(
            user_signal=(
                'The user just said: "Consolidate what you know about the new auth '
                'system right now — I need a coherent picture before we continue."'
            )
        )

    assert 'summarize_node' in response.verb.lower(), (
        f'Agent picked the wrong verb: {response.verb!r}. '
        'Expected memex_memory_summarize_node (synchronous, in-session need).'
    )


@pytest.mark.integration
@pytest.mark.llm
def test_real_agent_picks_summarize_node_on_conflict_signal():
    """TC7b: agent notices conflicting retrieved facts mid-conversation; the F5
    description steers it toward summarize_node BEFORE answering.
    """
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    import dspy

    predictor, lm = _build_predictor()

    with dspy.context(lm=lm):
        response = predictor(
            user_signal=(
                'The agent is mid-conversation. Retrieved facts about the entity '
                '"deploy target" include both "production" and "staging"; the user '
                'is asking a follow-up question that depends on the consolidated view.'
            )
        )

    assert 'summarize_node' in response.verb.lower(), (
        f'Agent picked the wrong verb on conflict signal: {response.verb!r}. '
        'F5 description should steer toward synchronous consolidation when '
        'retrieved facts conflict and the user asks a follow-up.'
    )
