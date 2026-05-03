"""F43 — MCP tool description content pinning.

Asserts that the F43 §3.5 5-step flow + §3.4.1 axes table + §3.4.2 historical
routing rule are present verbatim in the registered descriptions of
``memex_record_outcome`` and ``memex_memory_deprioritize``. Per CLAUDE.md
rule 24 (agent-surface parity), the MCP layer is the primary surface.

When the spec changes, these assertions fail — that is the contract.
Source: cognitive-memory-research-report.md §3.5 + §3.4.1 + §3.4.2 (added
2026-05-02).
"""

from __future__ import annotations

import pytest

from memex_mcp._f43_descriptions import (
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_RECORD_OUTCOME_DESCRIPTION,
)


# ---------------------------------------------------------------------------
# Required content checkpoints — sourced from spec §3.5 worked example.
# ---------------------------------------------------------------------------

# Step 1 disambiguation (§3.5 Step 1).
_S1_KEYWORDS = ('Disambiguate', 'ASK before writing')

# Step 2 routing — must teach Options A/B/C and the top_k>=30 rule.
_S2_KEYWORDS = (
    'memex_find_note',
    'memex_memory_search',
    'Option A',
    'Option B',
    'Option C',
    'memex_list_entities',
    'memex_get_entity_mentions',
    'memex_get_page_indices',
    'memex_get_memory_units',
    'top_k must be ≥30',
)

# Step 3 mandatory LLM judgment.
_S3_KEYWORDS = ('Mandatory LLM judgment', 'NEVER bulk-write')

# Steps 4+5 paired writes.
_S45_KEYWORDS = (
    'memex_record_outcome',
    'memex_memory_deprioritize',
    'SAME subset',
)

# Imperfect-recall framing + F33 safety net.
_IMPERFECT_KEYWORDS = (
    'Imperfect recall',
    'F33',
    'GRADIENT',
)

# Orthogonal-axes table from §3.4.1.
_AXES_KEYWORDS = (
    'orthogonal axes',
    'MW is the gradient',
    'binary',
    'Append-only counter',
    'memory_restore',
)

# Historical / audit-query routing rule (§3.4.2).
_HISTORICAL_KEYWORDS = (
    'memex_get_unit_history',
    'apply_pre_filter=False',
    'evolved',
    'used to',
    'history of',
    'audit',
    'show me everything',
)

# What is NOT a gap — codified scope-creep blockers.
_DO_NOT_ADD_KEYWORDS = (
    'memex_resolve',
    'resolved_at',
    'resolution_type',
    'bulk-by-source',
    'Note-level deprioritize',
)


def _assert_all_present(text: str, keywords: tuple[str, ...], section: str) -> None:
    missing = [kw for kw in keywords if kw not in text]
    assert not missing, (
        f'F43 description is missing {section} keywords: {missing!r}.\n'
        'See cognitive-memory-research-report.md §3.5 + §3.4.1 + §3.4.2.'
    )


@pytest.mark.parametrize(
    'description',
    [MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION, MEMEX_RECORD_OUTCOME_DESCRIPTION],
    ids=['deprioritize', 'record_outcome'],
)
def test_description_includes_5_step_flow(description: str) -> None:
    _assert_all_present(description, _S1_KEYWORDS, 'Step 1 (disambiguate)')
    _assert_all_present(description, _S2_KEYWORDS, 'Step 2 (routing + Options A/B/C)')
    _assert_all_present(description, _S3_KEYWORDS, 'Step 3 (mandatory LLM judgment)')
    _assert_all_present(description, _S45_KEYWORDS, 'Steps 4+5 (paired writes)')


@pytest.mark.parametrize(
    'description',
    [MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION, MEMEX_RECORD_OUTCOME_DESCRIPTION],
    ids=['deprioritize', 'record_outcome'],
)
def test_description_includes_imperfect_recall_framing(description: str) -> None:
    _assert_all_present(description, _IMPERFECT_KEYWORDS, 'imperfect-recall framing')


@pytest.mark.parametrize(
    'description',
    [MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION, MEMEX_RECORD_OUTCOME_DESCRIPTION],
    ids=['deprioritize', 'record_outcome'],
)
def test_description_includes_axes_table(description: str) -> None:
    _assert_all_present(description, _AXES_KEYWORDS, 'orthogonal-axes table')


@pytest.mark.parametrize(
    'description',
    [MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION, MEMEX_RECORD_OUTCOME_DESCRIPTION],
    ids=['deprioritize', 'record_outcome'],
)
def test_description_includes_historical_routing(description: str) -> None:
    _assert_all_present(description, _HISTORICAL_KEYWORDS, 'historical-routing rule')


@pytest.mark.parametrize(
    'description',
    [MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION, MEMEX_RECORD_OUTCOME_DESCRIPTION],
    ids=['deprioritize', 'record_outcome'],
)
def test_description_codifies_do_not_add_list(description: str) -> None:
    _assert_all_present(description, _DO_NOT_ADD_KEYWORDS, 'do-NOT-add list')


@pytest.mark.asyncio
async def test_record_outcome_tool_registered_with_f43_description() -> None:
    """The MCP server registers memex_record_outcome with the F43 description."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_record_outcome')
    assert tool is not None, 'memex_record_outcome tool not registered'
    assert tool.description == MEMEX_RECORD_OUTCOME_DESCRIPTION, (
        'memex_record_outcome registered description does not match the F43 '
        'description constant. Check server.py wiring.'
    )


@pytest.mark.asyncio
async def test_deprioritize_tool_registered_with_f43_description() -> None:
    """The MCP server registers memex_memory_deprioritize with the F43 description."""
    from memex_mcp.server import mcp

    tool = await mcp.get_tool('memex_memory_deprioritize')
    assert tool is not None, 'memex_memory_deprioritize tool not registered'
    assert tool.description == MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION, (
        'memex_memory_deprioritize registered description does not match the F43 '
        'description constant. Check server.py wiring.'
    )
