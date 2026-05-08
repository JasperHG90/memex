"""F43 — Hermes briefing resolution-flow primer pinning.

Asserts the Hermes session briefing carries the §3.5 5-step flow + §3.4.1
axes table + §3.4.2 historical-routing rule. Pure string-contains; the
matching ``@pytest.mark.llm``-driven verb-pair selection lives in
``test_int_f43_briefing_llm.py``.
"""

from __future__ import annotations

import pytest

from memex_hermes_plugin.memex.briefing import (
    _RESOLUTION_FLOW_PRIMER,
    format_briefing_block,
)
from memex_hermes_plugin.memex.templates import (
    HISTORICAL_ROUTING_PROMPT_FRAGMENT,
    RESOLUTION_FLOW_PROMPT_FRAGMENT,
)


@pytest.mark.parametrize(
    'kw',
    [
        '5-step flow',
        'Disambiguate',
        'memex_find_note',
        # Compressed form names the trio as "A: entity-anchored" / "B: ..." /
        # "C: ..."; the older "Option A/B/C" phrasing is also accepted (see
        # the keyword-alternatives logic below).
        ['Option A', 'A: entity-anchored'],
        ['Option B', 'B: cross-note semantic'],
        ['Option C', 'C: single-note PageIndex'],
        # The compressed primer renders the threshold with a space ("≥ 30");
        # accept either spacing so a later revision passes too.
        ['≥30', '≥ 30'],
        'memex_get_memory_units',
        # Compressed primer says "LLM-judge the candidates"; older phrasing
        # was "Mandatory LLM judgment".
        ['Mandatory LLM judgment', 'LLM-judge the candidates'],
        'memex_record_outcome',
        'memex_memory_deprioritize',
        # F33 ticket ref was intentionally stripped per the no-ticket-refs
        # policy. The concept ("exploration is the safety net") is what
        # remains; accept either phrasing.
        ['F33', 'exploration is the safety net'],
        'memex_get_unit_history',
        'apply_pre_filter=False',
        'evolved',
        'used to',
    ],
)
def test_resolution_flow_primer_includes_keyword(kw: str | list[str]) -> None:
    """The primer must mention each canonical §3.5 / §3.4.1 / §3.4.2 keyword.

    Each parametrize entry is either a single literal string or a list of
    acceptable alternatives — the latter is used for concepts where the
    compressed surface intentionally renames the phrasing (e.g. ``Option A``
    → ``A: entity-anchored``) so older and newer phrasings both pass.
    """
    candidates = [kw] if isinstance(kw, str) else list(kw)
    assert any(c in _RESOLUTION_FLOW_PRIMER for c in candidates), (
        f'Hermes resolution-flow primer is missing any of {candidates!r}. '
        'See cognitive-memory-research-report.md §3.5 / §3.4.1 / §3.4.2.'
    )


def test_format_briefing_block_includes_resolution_flow_primer() -> None:
    """The full briefing block (system prompt) embeds the primer."""
    out = format_briefing_block(
        briefing='',
        vault_id='vault-x',
        project_id='proj-x',
        session_note_key='session:test',
        kv_instructions_if_no_vault=False,
    )
    assert _RESOLUTION_FLOW_PRIMER in out, (
        'format_briefing_block must include the resolution-flow primer alongside '
        'the storage-model primer.'
    )


def test_resolution_flow_template_fragment_present() -> None:
    """The verb-pair scaffolding template fragment carries the 5-step flow."""
    assert 'DISAMBIGUATE' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'ROUTE' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'JUDGE' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'RECORD' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'DEPRIORITIZE' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'top_k>=30' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'memex_record_outcome' in RESOLUTION_FLOW_PROMPT_FRAGMENT
    assert 'memex_memory_deprioritize' in RESOLUTION_FLOW_PROMPT_FRAGMENT


def test_historical_routing_template_fragment_present() -> None:
    """The historical-routing fragment names F49 + apply_pre_filter=False."""
    assert 'memex_get_unit_history' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'apply_pre_filter=False' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'evolved' in HISTORICAL_ROUTING_PROMPT_FRAGMENT
    assert 'audit' in HISTORICAL_ROUTING_PROMPT_FRAGMENT


def test_hermes_record_outcome_schema_mentions_paired_writes() -> None:
    """The Hermes record_outcome tool description references the paired-write rule."""
    from memex_hermes_plugin.memex.tools import RECORD_OUTCOME_SCHEMA

    desc = RECORD_OUTCOME_SCHEMA['description']
    assert 'memex_memory_deprioritize' in desc, 'paired-write partner missing'
    assert '5-step flow' in desc or 'PAIRED writes' in desc, (
        'record_outcome schema should reference the §3.5 paired-write flow'
    )


def test_hermes_deprioritize_schema_mentions_paired_writes() -> None:
    """The Hermes deprioritize tool description references the paired-write rule."""
    from memex_hermes_plugin.memex.tools import MEMORY_DEPRIORITIZE_SCHEMA

    desc = MEMORY_DEPRIORITIZE_SCHEMA['description']
    assert 'memex_record_outcome' in desc, 'paired-write partner missing'
    assert 'PAIRED writes' in desc or '5-step flow' in desc, (
        'deprioritize schema should reference the §3.5 paired-write flow'
    )
