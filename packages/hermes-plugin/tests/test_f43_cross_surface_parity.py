"""F43 — cross-surface parity (post-compression).

After 2026-05-14: the 5-step resolution flow is owned by the MCP tool
descriptions (``memex_mcp._resolution_flow_descriptions``), authoritative
for all clients. The hermes-plugin tool schemas mirror those descriptions
(because hermes calls tools in-process and surfaces its own schemas to the
LLM). The hermes briefing and the Claude Code plugin rule no longer carry
the full flow — they delegate to the MCP descriptions.

Parity contract enforced here:

  1. MCP record_outcome + deprioritize descriptions carry every canonical concept.
  2. Hermes tool-schema descriptions mirror those concepts.

The hermes briefing and Claude Code rule are intentionally NOT parity targets
anymore; pointing at the tool descriptions is the contract for those surfaces.
"""

from __future__ import annotations

import pytest

pytest.importorskip('memex_mcp')

from memex_hermes_plugin.memex.tools import (  # noqa: E402
    MEMORY_DEPRIORITIZE_SCHEMA,
    RECORD_OUTCOME_SCHEMA,
)
from memex_mcp._resolution_flow_descriptions import (  # noqa: E402
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_RECORD_OUTCOME_DESCRIPTION,
)


def _surfaces() -> dict[str, str]:
    """Return the two surface texts that must carry the flow."""
    return {
        'mcp': MEMEX_RECORD_OUTCOME_DESCRIPTION + '\n' + MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
        'hermes_tools': (
            RECORD_OUTCOME_SCHEMA['description'] + '\n' + MEMORY_DEPRIORITIZE_SCHEMA['description']
        ),
    }


# Each entry is (concept_id, list-of-acceptable-phrasings, mode).
#   - mode='any': at least one phrasing must appear in the surface text.
#   - mode='all': every phrasing must appear (paired concepts).
_CONCEPTS: list[tuple[str, list[str], str]] = [
    ('options_abc_a', ['Option A', 'Options A/B/C', 'A entity-anchored'], 'any'),
    ('options_abc_b', ['Option B', 'Options A/B/C', 'B cross-note'], 'any'),
    ('options_abc_c', ['Option C', 'Options A/B/C', 'C single-note'], 'any'),
    (
        'top_k_at_least_30',
        ['top_k=30', 'top_k>=30', 'top_k >= 30', 'top_k must be ≥30', 'top_k must be **≥30**'],
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
    _CONCEPTS,
    ids=[c[0] for c in _CONCEPTS],
)
def test_concept_present_in_authoritative_surfaces(
    concept: str, phrase_options: list[str], mode: str
) -> None:
    """The flow concepts must appear in BOTH the MCP descriptions and the
    hermes tool-schema mirror. Failure here = real surface drift; fix the
    surface, do not relax the assertion."""
    surfaces = _surfaces()
    failures: list[str] = []
    for surface_name, text in surfaces.items():
        if mode == 'all':
            missing = [p for p in phrase_options if p not in text]
            if missing:
                failures.append(f'{surface_name!r}: missing all-of phrases {missing!r}')
        else:  # mode == 'any'
            if not any(p in text for p in phrase_options):
                failures.append(
                    f'{surface_name!r}: none of the phrasings {phrase_options!r} appeared'
                )
    assert not failures, f'F43 surface parity broke for concept {concept!r}:\n' + '\n'.join(
        '  - ' + f for f in failures
    )
