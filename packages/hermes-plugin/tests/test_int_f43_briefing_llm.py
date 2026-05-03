"""F43 — Real-LLM golden tests against the Hermes briefing primer.

Drives a real LLM agent turn ("Telegram notifications are now resolved") with
the Hermes session-briefing system block in scope (which carries the
``_RESOLUTION_FLOW_PRIMER`` + ``_STORAGE_MODEL_PRIMER`` per F43) and asserts
the same canonical §3.5 Step 1-5 expectations as the MCP-side test.

Skipped unless GEMINI_API_KEY / GOOGLE_API_KEY is set; uses the same model
pinning as the rest of the repo's LLM-gated tests
(gemini-3-flash-preview).

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

from memex_hermes_plugin.memex.briefing import format_briefing_block
from memex_hermes_plugin.memex.tools import (
    MEMORY_DEPRIORITIZE_SCHEMA,
    RECORD_OUTCOME_SCHEMA,
)

_HAS_GEMINI_KEY = bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'))
_HAS_LITELLM = _ilu.find_spec('litellm') is not None
_SKIP_LLM_TESTS = bool(os.environ.get('SKIP_LLM_TESTS'))


# ---------------------------------------------------------------------------
# Hermes tool bundle — what a Hermes turn surfaces at the LLM.
# ---------------------------------------------------------------------------


def _hermes_tools_with_routing() -> list[dict[str, Any]]:
    """Bundle Hermes record_outcome + deprioritize plus search/routing tools.

    The Hermes briefing primer carries the §3.5 flow; tool descriptions are
    shorter than MCP's because the briefing teaches routing.
    """

    def _tool(
        name: str,
        description: str,
        params: dict[str, Any],
        required: list[str] | None = None,
    ) -> dict[str, Any]:
        # The default `list(params.keys())[:1]` only marks the first parameter
        # required, which is wrong for tools whose required set is multi-arg.
        # Tools sourced from real schemas (RECORD_OUTCOME_SCHEMA etc.) use
        # `parameters` directly and bypass this helper. For tools defined here,
        # pass `required=` explicitly when the canonical required list is known
        # (mirrors the MCP-side pattern in test_int_f43_resolution_flow_llm.py).
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
        {
            'type': 'function',
            'function': {
                'name': RECORD_OUTCOME_SCHEMA['name'],
                'description': RECORD_OUTCOME_SCHEMA['description'],
                'parameters': RECORD_OUTCOME_SCHEMA['parameters'],
            },
        },
        {
            'type': 'function',
            'function': {
                'name': MEMORY_DEPRIORITIZE_SCHEMA['name'],
                'description': MEMORY_DEPRIORITIZE_SCHEMA['description'],
                'parameters': MEMORY_DEPRIORITIZE_SCHEMA['parameters'],
            },
        },
        _tool(
            'memex_find_note',
            'Find a note by title fragment.',
            {'query': {'type': 'string'}},
        ),
        _tool(
            'memex_memory_search',
            'Cross-note semantic search; pass top_k to broaden the result window (default 5).',
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
            'Memory units mentioning the given entity.',
            {'entity_id': {'type': 'string'}},
        ),
        _tool(
            'memex_get_unit_history',
            'Walk the contradiction graph backward (oldest -> newest).',
            {'unit_id': {'type': 'string'}},
        ),
    ]


def _briefing_system_message() -> str:
    """The same system block Hermes injects at session start."""
    return format_briefing_block(
        briefing='',
        vault_id='vault-test',
        project_id='proj-test',
        session_note_key='session:f43-llm-test',
        kv_instructions_if_no_vault=False,
    )


# ---------------------------------------------------------------------------
# Test (a) — disambiguate first.
# ---------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.skipif(_SKIP_LLM_TESTS, reason='SKIP_LLM_TESTS=1 set')
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_hermes_briefing_drives_disambiguation_on_ambiguous_prompt() -> None:
    import litellm

    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {'role': 'system', 'content': _briefing_system_message()},
                {
                    'role': 'system',
                    'content': (
                        'There are three reflection notes mentioning Telegram in the '
                        'last week (cron output, briefing alerts, media bug). The user '
                        'is making an ambiguous claim — apply the §3.5 Step 1 rule.'
                    ),
                },
                {
                    'role': 'user',
                    'content': 'The Telegram notifications are now resolved.',
                },
            ],
            tools=_hermes_tools_with_routing(),
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
    write_calls = [
        tc
        for tc in tool_calls
        if tc.function.name in ('memex_record_outcome', 'memex_memory_deprioritize')
    ]
    assert not write_calls, (
        f'agent issued a write ({[tc.function.name for tc in write_calls]}) on an '
        'ambiguous prompt; Hermes briefing §3.5 Step 1 breach.'
    )


# ---------------------------------------------------------------------------
# Test (b) — search before write when unit_ids are unknown.
# ---------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.skipif(_SKIP_LLM_TESTS, reason='SKIP_LLM_TESTS=1 set')
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_hermes_briefing_drives_search_before_write() -> None:
    import litellm

    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {'role': 'system', 'content': _briefing_system_message()},
                {
                    'role': 'system',
                    'content': (
                        'The user names a specific bug but you do NOT have unit_ids. '
                        'Apply §3.5 Step 2 routing: title-fragment -> memex_find_note '
                        'or content-only -> memex_memory_search BEFORE any write.'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        "The Telegram media handler bug from yesterday's reflection is fixed."
                    ),
                },
            ],
            tools=_hermes_tools_with_routing(),
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
    ), f'agent first-called {first!r}; Hermes briefing §3.5 Step 2 breach.'
    assert first not in (
        'memex_record_outcome',
        'memex_memory_deprioritize',
    ), 'agent wrote before searching — Hermes §3.5 Step 2 breach.'


# ---------------------------------------------------------------------------
# Test (c) — Option B uses top_k >= 30.
# ---------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.skipif(_SKIP_LLM_TESTS, reason='SKIP_LLM_TESTS=1 set')
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_hermes_briefing_option_b_uses_top_k_30() -> None:
    import litellm

    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {'role': 'system', 'content': _briefing_system_message()},
                {
                    'role': 'system',
                    'content': (
                        'Use Option B (cross-note memex_memory_search) per the §3.5 '
                        'flow. The briefing tells you top_k MUST be >= 30; the default '
                        '5 is too narrow.'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        'Run an Option-B cross-note search for "telegram media handler" '
                        'with after="2026-04-22". Pick top_k correctly.'
                    ),
                },
            ],
            tools=_hermes_tools_with_routing(),
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
        f'agent did not call memex_memory_search; calls: '
        f'{[tc.function.name for tc in tool_calls]!r}'
    )
    args = json.loads(search_calls[0].function.arguments)
    top_k = args.get('top_k')
    assert isinstance(top_k, int) and top_k >= 30, (
        f'agent set top_k={top_k!r}; Hermes briefing §3.5 Option B requires top_k>=30.'
    )


# ---------------------------------------------------------------------------
# Test (d) — paired writes against the LLM-judged-relevant subset only.
# ---------------------------------------------------------------------------


@pytest.mark.llm
@pytest.mark.skipif(_SKIP_LLM_TESTS, reason='SKIP_LLM_TESTS=1 set')
@pytest.mark.skipif(not _HAS_GEMINI_KEY, reason='GEMINI_API_KEY / GOOGLE_API_KEY not set')
@pytest.mark.skipif(not _HAS_LITELLM, reason='litellm not installed')
def test_hermes_briefing_paired_writes_against_subset() -> None:
    import litellm

    relevant_id = str(uuid.uuid4())
    irrelevant_id = str(uuid.uuid4())
    try:
        resp = litellm.completion(
            model='gemini/gemini-3-flash-preview',
            messages=[
                {'role': 'system', 'content': _briefing_system_message()},
                {
                    'role': 'system',
                    'content': (
                        'You have completed §3.5 Steps 1-3 and judged ONE unit '
                        'relevant. Issue PAIRED writes (memex_record_outcome with '
                        'success=false AND memex_memory_deprioritize) against ONLY '
                        'that unit — never the irrelevant one.'
                    ),
                },
                {
                    'role': 'user',
                    'content': (
                        'Telegram media bug is fixed. Candidates after Step 3:\n'
                        f'- {relevant_id}: "Telegram media handler crashes on .webp" '
                        '(RELEVANT)\n'
                        f'- {irrelevant_id}: "Worked on memex 3h today" (NOT RELEVANT)\n'
                        'Issue paired writes for the relevant unit only.'
                    ),
                },
            ],
            tools=_hermes_tools_with_routing(),
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

    assert 'memex_record_outcome' in names, f'§3.5 Step 4 breach. Calls: {names!r}'
    assert 'memex_memory_deprioritize' in names, f'§3.5 Step 5 breach. Calls: {names!r}'

    for tc in tool_calls:
        if tc.function.name not in ('memex_record_outcome', 'memex_memory_deprioritize'):
            continue
        args_str = tc.function.arguments
        assert irrelevant_id not in args_str, (
            f'agent wrote against the irrelevant unit; §3.5 Step 3 breach. '
            f'Tool: {tc.function.name}, args: {args_str!r}'
        )
        assert relevant_id in args_str, (
            f'agent did not target the relevant unit; tool: {tc.function.name}, args: {args_str!r}'
        )
