"""F43 — Real-LLM golden tests against the MCP tool descriptions.

Drives a real LLM agent turn ("Telegram notifications are now resolved") with
the F43 ``memex_record_outcome`` and ``memex_memory_deprioritize`` descriptions
in scope and asserts the canonical §3.5 5-step flow expectations:

  (a) Agent disambiguates first when scope is ambiguous.
  (b) Agent calls ``memex_find_note`` (or ``memex_memory_search``) BEFORE any
      write when the candidate set is unknown.
  (c) For cross-note scope using Option B, top_k is set to >=30.
  (d) Paired writes: ``memex_record_outcome(success=False)`` AND
      ``memex_memory_deprioritize`` are issued, and only against the
      LLM-judged-relevant subset (not every candidate).

Each assertion has a 30s per-call timeout per round-6 LOW deferral.
Skipped unless GEMINI_API_KEY / GOOGLE_API_KEY is set; uses the same model
pinning as the rest of the repo's LLM-gated tests
(gemini-3-flash-preview).

Note: Gemini's ``temperature=0`` is not strictly deterministic (the provider
has an implicit minimum temperature and outputs can still vary across calls).
Each test is wrapped with ``pytest.mark.flaky(reruns=2)`` so transient
output drift does not red the suite; the assertions themselves are kept tool-
name / numeric / UUID based so a one-off rephrasing does not falsely fail.

Per CLAUDE.md (Markers): ``@pytest.mark.llm`` so the tests are gated out of
the default CI lane.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
import uuid
from typing import Any

import pytest

from memex_mcp._resolution_flow_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_RECORD_OUTCOME_DESCRIPTION,
)

_HAS_GEMINI_KEY = bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'))
_HAS_LITELLM = _ilu.find_spec('litellm') is not None
_SKIP_LLM_TESTS = bool(os.environ.get('SKIP_LLM_TESTS'))


# ---------------------------------------------------------------------------
# Tool-schema bundle — what the LLM sees as the available tool set.
# ---------------------------------------------------------------------------


def _f43_tool_set() -> list[dict[str, Any]]:
    """Bundle the tools the F43 flow can pick from.

    Includes the pair under test (record_outcome + deprioritize) plus the
    routing tools (find_note, memory_search, list_entities,
    get_entity_mentions, get_page_indices, get_memory_units, get_unit_history)
    so the model has to actually pick by description, not by lonely-verb-in-list.
    """

    def _tool(
        name: str,
        description: str,
        params: dict[str, Any],
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        # The default `list(params.keys())[:1]` only marks the first parameter
        # required, which is wrong for tools whose required set is multi-arg
        # (e.g. memex_record_outcome needs BOTH success AND unit_ids in
        # memory_unit mode). Pass an explicit `required=` for those.
        return {
            'type': 'function',
            'function': {
                'name': name,
                'description': description,
                'parameters': {
                    'type': 'object',
                    'properties': params,
                    'required': required if required is not None else list(params.keys())[:1],
                },
            },
        }

    return [
        _tool(
            'memex_record_outcome',
            MEMEX_RECORD_OUTCOME_DESCRIPTION,
            {
                'success': {'type': 'boolean'},
                'unit_ids': {'type': 'array', 'items': {'type': 'string'}},
                'reason': {'type': 'string'},
            },
            required=['success', 'unit_ids'],
        ),
        _tool(
            'memex_memory_deprioritize',
            MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
            {
                'unit_id': {'type': 'string'},
                'reason': {'type': 'string'},
            },
            required=['unit_id'],
        ),
        _tool(
            'memex_find_note',
            'Find a note by title fragment (indexed, cheap).',
            {'query': {'type': 'string'}},
        ),
        _tool(
            'memex_memory_search',
            'Cross-note semantic search over memory units. Pass top_k to '
            'broaden the result window (default 5).',
            {
                'query': {'type': 'string'},
                'top_k': {'type': 'integer'},
                'after': {'type': 'string'},
                'apply_pre_filter': {'type': 'boolean'},
            },
        ),
        _tool(
            'memex_list_entities',
            'Search the entity graph by query string.',
            {'query': {'type': 'string'}},
        ),
        _tool(
            'memex_get_entity_mentions',
            'Return every memory unit mentioning the given entity.',
            {'entity_id': {'type': 'string'}},
        ),
        _tool(
            'memex_get_page_indices',
            'Return chunk-level layout summary for a single note.',
            {'note_id': {'type': 'string'}},
        ),
        _tool(
            'memex_get_memory_units',
            'Hydrate memory units by unit_ids OR chunk_ids.',
            {
                'unit_ids': {'type': 'array', 'items': {'type': 'string'}},
                'chunk_ids': {'type': 'array', 'items': {'type': 'string'}},
            },
        ),
        _tool(
            'memex_get_unit_history',
            'Walk the contradiction graph backward from a unit (oldest -> newest).',
            {'unit_id': {'type': 'string'}},
        ),
    ]


# ---------------------------------------------------------------------------
# Test (a) — agent disambiguates first when scope is ambiguous.
# ---------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.flaky(reruns=2, reruns_delay=1)
@pytest.mark.skipif(_SKIP_LLM_TESTS, reason='SKIP_LLM_TESTS=1 set')
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_ambiguous_resolution_prompts_clarification_before_write() -> None:
    """Step 1 — disambiguate first.

    Given an ambiguous prompt ("Telegram notifications are now resolved" — could
    be the reflection cron, the briefing alerts, or the media-handling bug),
    the agent should ASK before issuing any write.
    """
    import litellm

    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You are an agent integrated with Memex. You have several '
                        'reflection notes mentioning Telegram in the last week:\n'
                        '- "Daily reflection 2026-04-29" mentions Telegram cron output\n'
                        '- "Daily reflection 2026-04-30" mentions Telegram briefing alerts\n'
                        '- "Debugging session 2026-04-30" mentions a Telegram media bug\n'
                        'Pick a tool to call. If the scope is ambiguous, the §3.5 flow '
                        'requires you to ASK the user FIRST rather than write. Only call '
                        'memex_record_outcome / memex_memory_deprioritize after you know '
                        'exactly which units the user means.'
                    ),
                },
                {
                    'role': 'user',
                    'content': 'The Telegram notifications are now resolved.',
                },
            ],
            tools=_f43_tool_set(),
            tool_choice='auto',
            temperature=0,
            timeout=30,
            api_key=os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'),
        )
    except Exception as exc:
        if 'rate' in str(exc).lower() or '429' in str(exc):
            pytest.skip(f'LLM rate-limited: {exc}')
        raise

    msg = resp.choices[0].message
    tool_calls = msg.tool_calls or []
    write_calls = [
        tc
        for tc in tool_calls
        if tc.function.name in ('memex_record_outcome', 'memex_memory_deprioritize')
    ]
    assert not write_calls, (
        f'Agent issued a write call ({[tc.function.name for tc in write_calls]}) '
        'against an ambiguous scope without disambiguating first. §3.5 Step 1 '
        'breach.'
    )


# ---------------------------------------------------------------------------
# Test (b) — agent calls find_note (or memory_search) BEFORE any write.
# ---------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.flaky(reruns=2, reruns_delay=1)
@pytest.mark.skipif(_SKIP_LLM_TESTS, reason='SKIP_LLM_TESTS=1 set')
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_resolution_calls_search_before_write_when_no_unit_ids_known() -> None:
    """Step 2 — search before write.

    Given a concrete-but-no-unit-ids prompt, the FIRST tool call should be a
    routing/search call, not a write.
    """
    import litellm

    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You are an agent integrated with Memex. The user is naming a '
                        'specific bug ("Telegram media handler bug from yesterday\'s '
                        'reflection") but you do NOT have memory unit IDs. Per the §3.5 '
                        'flow, route by info quality: title-fragment known -> '
                        'memex_find_note; content only -> memex_memory_search. Only '
                        'after you have a candidate set should you write.'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        "The Telegram media handler bug from yesterday's reflection is fixed."
                    ),
                },
            ],
            tools=_f43_tool_set(),
            tool_choice='auto',
            temperature=0,
            timeout=30,
            api_key=os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'),
        )
    except Exception as exc:
        if 'rate' in str(exc).lower() or '429' in str(exc):
            pytest.skip(f'LLM rate-limited: {exc}')
        raise

    tool_calls = resp.choices[0].message.tool_calls or []
    assert tool_calls, f'no tool calls: {resp.choices[0].message!r}'
    first = tool_calls[0].function.name
    assert first in (
        'memex_find_note',
        'memex_memory_search',
        'memex_list_entities',
    ), (
        f'agent first-called {first!r}; §3.5 Step 2 requires a routing/search call '
        'before any write when unit_ids are unknown.'
    )
    # Negative assertion: no write call leads.
    assert first not in (
        'memex_record_outcome',
        'memex_memory_deprioritize',
    ), 'agent wrote before searching — §3.5 Step 2 breach.'


# ---------------------------------------------------------------------------
# Test (c) — Option B uses top_k >= 30.
# ---------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.flaky(reruns=2, reruns_delay=1)
@pytest.mark.skipif(_SKIP_LLM_TESTS, reason='SKIP_LLM_TESTS=1 set')
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_cross_note_search_uses_top_k_at_least_30() -> None:
    """Option B — cross-note semantic with top_k >= 30."""
    import litellm

    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You are an agent integrated with Memex. Use Option B '
                        '(cross-note memex_memory_search) to gather candidates for the '
                        '§3.5 resolution flow. The §3.5 spec says top_k MUST be >= 30; '
                        'the default 5 is too narrow.'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        'Do an Option-B cross-note semantic search for "telegram media '
                        'handler" with after="2026-04-22" so we can resolve units '
                        'across multiple notes. Pick top_k correctly.'
                    ),
                },
            ],
            tools=_f43_tool_set(),
            tool_choice='auto',
            temperature=0,
            timeout=30,
            api_key=os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'),
        )
    except Exception as exc:
        if 'rate' in str(exc).lower() or '429' in str(exc):
            pytest.skip(f'LLM rate-limited: {exc}')
        raise

    tool_calls = resp.choices[0].message.tool_calls or []
    search_calls = [tc for tc in tool_calls if tc.function.name == 'memex_memory_search']
    assert search_calls, (
        f'agent did not call memex_memory_search (Option B): '
        f'{[tc.function.name for tc in tool_calls]!r}'
    )
    args = json.loads(search_calls[0].function.arguments)
    top_k = args.get('top_k')
    assert isinstance(top_k, int), (
        f'agent did not set top_k (got {top_k!r}); §3.5 Option B requires top_k>=30.'
    )
    assert top_k >= 30, (
        f'agent set top_k={top_k}; §3.5 Option B requires top_k>=30 for cross-note '
        'recall. Default 5 misses paraphrased mentions.'
    )


# ---------------------------------------------------------------------------
# Test (d) — paired writes against the LLM-judged-relevant subset only.
# ---------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.flaky(reruns=2, reruns_delay=1)
@pytest.mark.skipif(_SKIP_LLM_TESTS, reason='SKIP_LLM_TESTS=1 set')
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_resolution_issues_paired_writes_against_subset() -> None:
    """Steps 4+5 — paired writes against the LLM-judged-relevant subset only."""
    import litellm

    relevant_id = str(uuid.uuid4())
    irrelevant_id = str(uuid.uuid4())
    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        'You are an agent integrated with Memex executing §3.5 Steps '
                        '4+5. The user has confirmed a bug fix; you have already done '
                        'Steps 1-3 and judged exactly ONE unit relevant. Issue PAIRED '
                        'writes (memex_record_outcome with success=false AND '
                        'memex_memory_deprioritize) against ONLY the relevant unit_id. '
                        'Do NOT write against the irrelevant one — that breaches Step 3 '
                        '(LLM judgment).'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        'Telegram media bug is fixed. Candidate units after Step 3 '
                        f'judgment:\n'
                        f'- {relevant_id}: "Telegram media handler crashes on .webp" '
                        '(RELEVANT)\n'
                        f'- {irrelevant_id}: "Worked on memex 3h today" (NOT RELEVANT — '
                        'episodic noise)\n'
                        'Issue paired writes for the relevant unit only.'
                    ),
                },
            ],
            tools=_f43_tool_set(),
            tool_choice='auto',
            temperature=0,
            timeout=30,
            api_key=os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'),
        )
    except Exception as exc:
        if 'rate' in str(exc).lower() or '429' in str(exc):
            pytest.skip(f'LLM rate-limited: {exc}')
        raise

    tool_calls = resp.choices[0].message.tool_calls or []
    names = [tc.function.name for tc in tool_calls]

    assert 'memex_record_outcome' in names, (
        f'agent did not call record_outcome; §3.5 Step 4 breach. Calls: {names!r}'
    )
    assert 'memex_memory_deprioritize' in names, (
        f'agent did not call memex_memory_deprioritize; §3.5 Step 5 breach. Calls: {names!r}'
    )

    # All write-call args must reference the relevant unit and never the irrelevant.
    for tc in tool_calls:
        if tc.function.name not in ('memex_record_outcome', 'memex_memory_deprioritize'):
            continue
        args_str = tc.function.arguments
        assert irrelevant_id not in args_str, (
            f'agent wrote against the irrelevant (episodic) unit; §3.5 Step 3 breach. '
            f'Tool: {tc.function.name}, args: {args_str!r}'
        )
        assert relevant_id in args_str, (
            f'agent did not target the relevant unit; tool: {tc.function.name}, args: {args_str!r}'
        )
