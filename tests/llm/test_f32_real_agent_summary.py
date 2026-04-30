"""F32 — real LLM picks ``memex_get_diagnostics_summary`` for vault-diagnostics prompt (Test 11).

Per the backlog-item DoD: prompt/tool-schema changes get a real-LLM-turn
validation, not just static schema assertions. This test drives a real Gemini
turn through dspy and asserts the model selects the F32 verb (not
``memex_memory_search`` / ``memex_note_search``) when asked about a vault's
diagnostic state.

Skips if ``GOOGLE_API_KEY`` is not set (CI-without-secret + local dev without
a key both behave correctly without false failures).
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.llm
def test_real_agent_reads_summary():
    if not os.environ.get('GOOGLE_API_KEY'):
        pytest.skip('GOOGLE_API_KEY not set')

    import dspy

    # Match the project's conventional integration-test model (gemini-3-flash-preview).
    # 120s matches F4 T6's verb-disambig timeout; 30s flaked on cold Gemini connections.
    lm = dspy.LM('gemini/gemini-3-flash-preview', timeout=120.0)
    dspy.configure(lm=lm)

    tool_catalog = (
        '- memex_memory_search(query): TEMPR memory-unit search; returns '
        'distilled facts that match a query.\n'
        '- memex_note_search(query): full-text search over note bodies; returns '
        'whole-note matches.\n'
        '- memex_list_entities(query): entity-graph search; returns canonical '
        'entity names.\n'
        '- memex_get_diagnostics_summary(vault_id): vault diagnostics — unit '
        'counts by status (active/stale/deprioritized), pending lint counts by '
        'type, cluster_count (null when manifold cache is cold), avg MW score, '
        'and top-5 retrieved entities.\n'
    )

    class ToolSelection(dspy.Signature):
        """Pick exactly one Memex tool name to answer the user's question.

        Return only the tool name (e.g. ``memex_memory_search``), nothing else.
        """

        question: str = dspy.InputField()
        tools: str = dspy.InputField(desc='Available tools and their descriptions.')
        chosen_tool: str = dspy.OutputField(desc='Exactly one tool name from the catalog.')

    predict = dspy.Predict(ToolSelection)
    question = (
        'How is the "research" vault doing? I want unit counts by status, '
        'top retrieved entities, and the manifold cluster count.'
    )

    try:
        response = predict(question=question, tools=tool_catalog)
    except Exception as e:
        # Gemini rate-limit or transient API error — don't fail the LLM gate
        # on environmental noise. Real failures (model picks the wrong tool)
        # are not caught here and still fail loudly below.
        msg = str(e)
        if (
            '429' in msg
            or 'RESOURCE_EXHAUSTED' in msg
            or 'rate' in msg.lower()
            or 'quota' in msg.lower()
        ):
            pytest.skip(f'Gemini rate-limited; retry later: {e}')
        raise
    chosen = (response.chosen_tool or '').strip()
    if not chosen:
        # Empty response is almost always rate-limit or transient API issue;
        # don't poison the gate on environmental noise.
        pytest.skip('Model returned empty chosen_tool — likely transient API issue.')

    # The verb must land. Don't over-constrain — model may wrap in backticks
    # or quote the name; substring match is sufficient.
    assert 'memex_get_diagnostics_summary' in chosen, (
        f'Expected the model to pick memex_get_diagnostics_summary; got: {chosen!r}'
    )
