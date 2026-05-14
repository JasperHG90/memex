"""F43 — cross-surface parity (post-2026-05-14 three-tier architecture).

Before 2026-05-14: the 5-step resolution flow was duplicated across the MCP
``_resolution_flow_descriptions`` constants AND the hermes-plugin tool
schemas. The test enforced both copies stayed in sync.

After 2026-05-14: there is exactly one copy — ``compose_universal()`` from
``memex_common.agent_surface`` (Tier 1b). The MCP and hermes tool descriptions
(Tier 1a) are intentionally TERSE — per-tool contract only, no multi-step
flow. This test enforces:

1. ``compose_universal()`` carries every flow concept (universal SSOT).
2. MCP descriptions (Tier 1a) do NOT carry the multi-step flow (boundary).
3. Hermes tool schemas re-export the MCP descriptions by identity (no drift).

The architecture-boundary fence for the MCP description content lives in
``test_briefing_f43_resolution_flow.py``; this file checks the SSOT itself
plus the cross-package identity invariant.
"""

from __future__ import annotations

import pytest

pytest.importorskip('memex_mcp')

from memex_common.agent_surface import compose_universal  # noqa: E402
from memex_common.tool_descriptions import (  # noqa: E402
    MEMEX_MEMORY_DEPRIORITIZE_DESC,
    MEMEX_RECORD_OUTCOME_DESC,
)
from memex_hermes_plugin.memex.tools import (  # noqa: E402
    MEMORY_DEPRIORITIZE_SCHEMA,
    RECORD_OUTCOME_SCHEMA,
)


# Each entry is (concept_id, list-of-acceptable-phrasings, mode).
#   - mode='any': at least one phrasing must appear in compose_universal().
#   - mode='all': every phrasing must appear (paired concepts).
_UNIVERSAL_CONCEPTS: list[tuple[str, list[str], str]] = [
    ('options_abc_a', ['Option A', 'A entity-anchored'], 'any'),
    ('options_abc_b', ['Option B', 'B cross-note'], 'any'),
    ('options_abc_c', ['Option C', 'C single-note'], 'any'),
    (
        'top_k_at_least_30',
        ['top_k=30', 'top_k>=30', 'top_k >= 30', '≥30'],
        'any',
    ),
    (
        'paired_writes',
        ['memex_record_outcome', 'memex_memory_deprioritize'],
        'all',
    ),
    (
        'apply_pre_filter_false',
        ['apply_pre_filter=False', 'apply_pre_filter = False'],
        'any',
    ),
]


@pytest.mark.parametrize(
    'concept,phrase_options,mode',
    _UNIVERSAL_CONCEPTS,
    ids=[c[0] for c in _UNIVERSAL_CONCEPTS],
)
def test_concept_present_in_universal_block(
    concept: str, phrase_options: list[str], mode: str
) -> None:
    """The universal block (Tier 1b SSOT) must carry every flow concept."""
    text = compose_universal()
    if mode == 'all':
        missing = [p for p in phrase_options if p not in text]
        assert not missing, (
            f'compose_universal() missing all-of phrases {missing!r} for concept {concept!r}'
        )
    else:  # mode == 'any'
        assert any(p in text for p in phrase_options), (
            f'compose_universal() carries none of {phrase_options!r} for concept {concept!r}'
        )


def test_record_outcome_description_is_terse() -> None:
    """MCP record_outcome description (Tier 1a) must NOT carry the
    multi-step universal flow scaffolding."""
    banned = ('Option A', 'Option B', 'Option C', 'Disambiguate', 'apply_pre_filter=False')
    leaked = [p for p in banned if p in MEMEX_RECORD_OUTCOME_DESC]
    assert not leaked, (
        f'Tier 1a record_outcome description leaks Tier 1b content {leaked!r}. '
        'Move it to memex_common.agent_surface.'
    )


def test_deprioritize_description_is_terse() -> None:
    """MCP deprioritize description (Tier 1a) must NOT carry the
    multi-step universal flow scaffolding."""
    banned = ('Option A', 'Option B', 'Option C', 'Disambiguate', 'apply_pre_filter=False')
    leaked = [p for p in banned if p in MEMEX_MEMORY_DEPRIORITIZE_DESC]
    assert not leaked, (
        f'Tier 1a deprioritize description leaks Tier 1b content {leaked!r}. '
        'Move it to memex_common.agent_surface.'
    )


def test_hermes_schemas_reexport_common_descriptions_by_identity() -> None:
    """Hermes tool schemas must use the same description objects as MCP —
    imported from ``memex_common.tool_descriptions``. Identity check is the
    strictest possible drift guard."""
    assert RECORD_OUTCOME_SCHEMA['description'] is MEMEX_RECORD_OUTCOME_DESC, (
        'Hermes RECORD_OUTCOME_SCHEMA description drifted from common SSOT.'
    )
    assert MEMORY_DEPRIORITIZE_SCHEMA['description'] is MEMEX_MEMORY_DEPRIORITIZE_DESC, (
        'Hermes MEMORY_DEPRIORITIZE_SCHEMA description drifted from common SSOT.'
    )
