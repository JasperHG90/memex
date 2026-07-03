"""F4 — deprioritize verb-pair invariant (Wave 0 §6 #12).

After 2026-05-14 compression: the deprioritize-vs-archive verb-pair lives
in the MCP ``memex_memory_deprioritize`` description (authoritative for all
clients) and the hermes-plugin tool-schema mirror.

Pure string-contains; the matching `@pytest.mark.llm` driven-agent verb
selection lives in `tests/test_e2e_f4_llm_turn.py`.
"""

from __future__ import annotations

from memex_mcp._deprioritize_descriptions import MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION
from memex_hermes_plugin.memex.tools import MEMORY_DEPRIORITIZE_SCHEMA


def test_mcp_description_mentions_deprioritize_verb():
    assert 'deprioritize' in MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION


def test_mcp_description_marks_deprioritize_as_non_destructive():
    """The MCP description must state non-destructive (the load-bearing distinction)."""
    text = MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION
    assert 'non-destructive' in text.lower() or 'NON-DESTRUCTIVE' in text


def test_hermes_schema_contrasts_against_destructive_archive():
    """The hermes-side schema is where deprioritize is contrasted against archive
    (per Wave 0 §6 #12). The MCP description focuses on what deprioritize *does*;
    the hermes schema spells out the verb-pair distinction for the agent."""
    desc = MEMORY_DEPRIORITIZE_SCHEMA['description'].lower()
    assert 'archive' in desc
    assert 'destructive' in desc


def test_hermes_schema_does_not_cross_wire_archive_as_deprioritize_alt() -> None:
    """Negative assertion: the schema must NOT suggest archive as a
    same-purpose alternative to deprioritize. Wave 0 §6 #12 keeps these
    verbs distinct.
    """
    lower = MEMORY_DEPRIORITIZE_SCHEMA['description'].lower()
    forbidden_phrases = (
        'use archive instead of deprioritize',
        'archive is equivalent to deprioritize',
        'archive or deprioritize',
    )
    for p in forbidden_phrases:
        assert p not in lower, f'Schema cross-wires archive: contains "{p}"'
