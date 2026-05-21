"""Per-tool description SSOT discipline tests.

Pins token budgets and load-bearing keywords for ``memex_common.tool_descriptions``.
Both the MCP server and the Hermes plugin import these constants — so
changing one constant updates both surfaces atomically. The verbatim test
below catches accidental edits.
"""

from __future__ import annotations

import pytest

from memex_common import tool_descriptions as td


# ---------------------------------------------------------------------------
# Budget — ≤300 tokens / ≤1,200 chars per tool description (Tier 1a).
# ---------------------------------------------------------------------------


_PER_TOOL_CHAR_CAP = 1_200


@pytest.mark.parametrize('name', td.__all__)
def test_tool_description_within_budget(name: str) -> None:
    val = getattr(td, name)
    assert isinstance(val, str)
    assert len(val) <= _PER_TOOL_CHAR_CAP, (
        f'{name} is {len(val)} chars (~{len(val) // 4} tokens), exceeding '
        f'cap {_PER_TOOL_CHAR_CAP}. Trim — tool descriptions are Tier 1a, '
        'agent-facing per-turn context. Composition-flow content belongs '
        'in memex_common.agent_surface (Tier 1b).'
    )


# ---------------------------------------------------------------------------
# Required content — the 4xx-triggering invariants that MUST be visible.
# ---------------------------------------------------------------------------


def test_record_outcome_warns_about_bare_success() -> None:
    """The V11 contract: bare success= without units returns 400. This
    line is the only thing preventing agents from emitting the legacy
    shape and getting rejected. Do NOT drop it."""
    desc = td.MEMEX_RECORD_OUTCOME_DESC
    assert 'success=True' in desc or 'bare `success' in desc
    assert '400' in desc
    assert 'units=[{' in desc


def test_record_outcome_specifies_verb_enum() -> None:
    desc = td.MEMEX_RECORD_OUTCOME_DESC
    for verb in ('helpful', 'not_helpful', 'not_used'):
        assert verb in desc


def test_deprioritize_warns_about_observation_uuid_400() -> None:
    """V21: observation UUIDs return HTTP 400 with source_memory_units, not 404.

    The description must mention the new structured-400 contract so the
    agent can redirect against the listed MU IDs.
    """
    desc = td.MEMEX_MEMORY_DEPRIORITIZE_DESC
    assert 'virtual' in desc.lower()
    assert 'unit_metadata.virtual' in desc
    assert 'HTTP 400' in desc or '400' in desc
    assert 'source_memory_units' in desc


def test_deprioritize_marks_non_destructive() -> None:
    desc = td.MEMEX_MEMORY_DEPRIORITIZE_DESC
    assert 'NON-DESTRUCTIVE' in desc or 'non-destructive' in desc.lower()


def test_kv_put_specifies_namespace_regex() -> None:
    """The namespace prefix is server-enforced; the description must
    spell out the regex so the agent constructs valid keys."""
    desc = td.MEMEX_KV_PUT_DESC
    for prefix in (
        'global:',
        'user:',
        'project:<id>:',
        'app:<app-id>:',
        'procedure:<verb>:<context-tag>',
    ):
        assert prefix in desc
    assert '400' in desc  # rejection on invalid prefix


def test_kv_put_rejects_content_facts() -> None:
    """Regression fence — KV is for operational pointers, NOT content facts.
    See packages/cli/tests/test_kv_prose_drift_guard.py for the broader
    drift guard. This test ensures the per-tool description carries the
    pointer-vs-fact distinction."""
    desc = td.MEMEX_KV_PUT_DESC
    assert 'NOT for content facts' in desc or 'NOT for facts' in desc
    assert 'memex_add_note' in desc  # the right alternative


def test_summarize_node_documents_rate_limit_envelope() -> None:
    desc = td.MEMEX_MEMORY_SUMMARIZE_NODE_DESC
    assert 'rate-limit' in desc.lower() or 'rate limit' in desc.lower()
    assert 'retry_after_seconds' in desc


def test_summarize_node_marked_synchronous() -> None:
    desc = td.MEMEX_MEMORY_SUMMARIZE_NODE_DESC
    assert 'SYNCHRONOUSLY' in desc or 'synchronous' in desc.lower()


def test_reconsolidate_vs_consolidate_distinction() -> None:
    """Both descriptions must teach the entity-vs-vault distinction so the
    agent picks the right verb."""
    recon = td.MEMEX_MEMORY_RECONSOLIDATE_DESC
    con = td.MEMEX_MEMORY_CONSOLIDATE_DESC
    assert 'memex_memory_consolidate' in recon, 'reconsolidate must cross-reference consolidate'
    assert 'memex_memory_reconsolidate' in con, 'consolidate must cross-reference reconsolidate'


# ---------------------------------------------------------------------------
# Constants are exported — accidental rename / removal breaks importers.
# ---------------------------------------------------------------------------


_REQUIRED_CONSTANTS = (
    'MEMEX_RECORD_OUTCOME_DESC',
    'MEMEX_MEMORY_DEPRIORITIZE_DESC',
    'MEMEX_MEMORY_RESTORE_DESC',
    'MEMEX_MEMORY_SUMMARIZE_NODE_DESC',
    'MEMEX_MEMORY_RECONSOLIDATE_DESC',
    'MEMEX_MEMORY_CONSOLIDATE_DESC',
    'MEMEX_KV_PUT_DESC',
    'MEMEX_KV_GET_DESC',
    'MEMEX_KV_SEARCH_DESC',
    'MEMEX_KV_LIST_DESC',
)


@pytest.mark.parametrize('name', _REQUIRED_CONSTANTS)
def test_constant_exists_and_non_empty(name: str) -> None:
    val = getattr(td, name)
    assert isinstance(val, str)
    assert val.strip()
