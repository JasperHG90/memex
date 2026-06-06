"""Identity-pin every Hermes schema description against the common SSOT.

The Hermes plugin mirrors several MCP tool schemas in-process. After the
2026-05-14 three-tier refactor the per-tool ``description`` strings live
in ``memex_common.tool_descriptions`` (Tier 1a SSOT) and BOTH MCP and
Hermes import them. The load-bearing invariant is **object identity**,
not just equality: a future contributor could paste a string literal
back into a Hermes schema and CI would not catch it on an equality
check (the strings happen to match for one revision, then drift later).

This file iterates every ``MEMEX_*_DESC`` re-exported by Hermes' tools
module and asserts the schema's ``description`` field is the same Python
object as the canonical constant.
"""

from __future__ import annotations

import pytest

from memex_common import tool_descriptions as common_descs
from memex_hermes_plugin.memex import tools as hermes_tools


# (canonical_constant_name, hermes_schema_name) — every Hermes schema
# whose `description=` field references a `tool_descriptions` constant.
# Add a new row here when a new tool migrates to the SSOT.
_PAIRINGS: list[tuple[str, str]] = [
    ('MEMEX_KV_PUT_DESC', 'KV_PUT_SCHEMA'),
    ('MEMEX_KV_GET_DESC', 'KV_GET_SCHEMA'),
    ('MEMEX_KV_SEARCH_DESC', 'KV_SEARCH_SCHEMA'),
    ('MEMEX_KV_LIST_DESC', 'KV_LIST_SCHEMA'),
    ('MEMEX_MEMORY_DEPRIORITIZE_DESC', 'MEMORY_DEPRIORITIZE_SCHEMA'),
    ('MEMEX_MEMORY_RESTORE_DESC', 'MEMORY_RESTORE_SCHEMA'),
    ('MEMEX_MEMORY_SUMMARIZE_NODE_DESC', 'MEMORY_SUMMARIZE_NODE_SCHEMA'),
    ('MEMEX_RECORD_OUTCOME_DESC', 'RECORD_OUTCOME_SCHEMA'),
    ('MEMEX_MEMORY_RECONSOLIDATE_DESC', 'MEMORY_RECONSOLIDATE_SCHEMA'),
    ('MEMEX_MEMORY_CONSOLIDATE_DESC', 'MEMORY_CONSOLIDATE_SCHEMA'),
    ('MEMEX_LIST_LINT_ACTIONS_DESC', 'LIST_LINT_ACTIONS_SCHEMA'),
    ('MEMEX_SUBMIT_LINT_PROPOSAL_DESC', 'SUBMIT_LINT_PROPOSAL_SCHEMA'),
]


@pytest.mark.parametrize('constant_name,schema_name', _PAIRINGS, ids=[p[1] for p in _PAIRINGS])
def test_hermes_schema_description_is_ssot_object(constant_name: str, schema_name: str) -> None:
    """Every Hermes schema's ``description`` field MUST be the same Python
    object as the canonical ``memex_common.tool_descriptions.MEMEX_*_DESC``
    constant. Equality is insufficient — a literal copy passes equality
    today but lets drift slip through the next time the SSOT changes."""
    canonical = getattr(common_descs, constant_name)
    schema = getattr(hermes_tools, schema_name)
    desc = schema['description']
    assert desc is canonical, (
        f'{schema_name}["description"] is not the canonical '
        f'`memex_common.tool_descriptions.{constant_name}` object — '
        'replace the inline string with the imported constant.'
    )


def test_every_tool_descriptions_export_has_a_hermes_pairing() -> None:
    """If ``memex_common.tool_descriptions`` exports a new ``MEMEX_*_DESC``
    constant, the corresponding Hermes schema MUST be added to ``_PAIRINGS``
    above. Today every export is mirrored — no legitimate exclusions exist.
    If a real exclusion arises later, re-introduce an exclusion set with a
    paired test that exercises the path (YAGNI until then)."""
    paired = {p[0] for p in _PAIRINGS}
    exported = {n for n in common_descs.__all__ if n.startswith('MEMEX_')}
    unaccounted = exported - paired
    assert not unaccounted, (
        f'New `MEMEX_*_DESC` exports without a Hermes identity pairing: '
        f'{sorted(unaccounted)!r}. Add them to `_PAIRINGS` in this test.'
    )
