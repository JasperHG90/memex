"""F43 — cross-surface parity test.

Asserts that the canonical §3.5 / §3.4.2 concepts appear in ALL FOUR agent
surfaces:

  1. MCP tool descriptions (``memex_mcp._f43_descriptions``)
  2. Hermes session-briefing primer (``memex_hermes_plugin.memex.briefing``)
  3. Claude Code plugin rule (``packages/claude-code-plugin/rules/memory-resolution-flow.md``)
  4. Hermes tool-schema descriptions (``memex_hermes_plugin.memex.tools``) —
     concise paraphrase rendered as the LLM's tool-listing entries; required
     so the verb-pair entries on Hermes match the briefing primer above.

Per CLAUDE.md rule 24 (agent-surface parity): the resolution-flow guidance must
not drift between surfaces. A failing assertion here indicates a real surface
drift — fix the surface, do not relax the assertion.

Source: cognitive-memory-research-report.md §3.5 + §3.4.1 + §3.4.2
(added 2026-05-02).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip('memex_mcp')

from memex_hermes_plugin.memex.briefing import _RESOLUTION_FLOW_PRIMER  # noqa: E402
from memex_hermes_plugin.memex.tools import (  # noqa: E402
    MEMORY_DEPRIORITIZE_SCHEMA,
    RECORD_OUTCOME_SCHEMA,
)
from memex_mcp._f43_descriptions import (  # noqa: E402
    MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
    MEMEX_RECORD_OUTCOME_DESCRIPTION,
)

_CC_RULE_PATH = (
    Path(__file__).parents[2] / 'claude-code-plugin' / 'rules' / 'memory-resolution-flow.md'
)


def _surfaces() -> dict[str, str]:
    """Return the four surface texts keyed by surface name.

    The MCP and Hermes-tools surfaces concatenate both descriptions because
    some concepts naturally appear on only one of the two verbs in some
    phrasings — the parity contract is "present somewhere in the verb pair",
    not "present in each verb individually" (the per-verb parity is enforced
    by test_f43_descriptions.py).
    """
    return {
        'mcp': MEMEX_RECORD_OUTCOME_DESCRIPTION + '\n' + MEMEX_MEMORY_DEPRIORITIZE_DESCRIPTION,
        'hermes': _RESOLUTION_FLOW_PRIMER,
        'claude_code': _CC_RULE_PATH.read_text(),
        'hermes_tools': (
            RECORD_OUTCOME_SCHEMA['description'] + '\n' + MEMORY_DEPRIORITIZE_SCHEMA['description']
        ),
    }


# Each entry is (concept_id, list-of-acceptable-phrasings, mode).
#   - mode='any': at least one phrasing must appear in the surface text.
#   - mode='all': every phrasing must appear (used for paired concepts where
#     both verbs / both phrases must co-occur).
# The Hermes tool-schema surface uses a concise "Options A/B/C" shorthand for
# the routing trio, so each per-letter concept accepts that shorthand as an
# acceptable phrasing.
_CONCEPTS: list[tuple[str, list[str], str]] = [
    ('options_abc_a', ['Option A', 'Options A/B/C'], 'any'),
    ('options_abc_b', ['Option B', 'Options A/B/C'], 'any'),
    ('options_abc_c', ['Option C', 'Options A/B/C'], 'any'),
    (
        'top_k_at_least_30',
        ['top_k=30', 'top_k>=30', 'top_k >= 30', 'top_k must be ≥30', '`top_k` must be **≥30**'],
        'any',
    ),
    (
        'paired_writes',
        ['memex_record_outcome', 'memex_memory_deprioritize'],
        'all',
    ),
    (
        'f33_safety_net',
        ['F33 exploration is the safety net', 'F33 exploration', 'exploration safety net'],
        'any',
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
def test_concept_present_in_all_surfaces(
    concept: str, phrase_options: list[str], mode: str
) -> None:
    """Every canonical concept must appear in every agent surface.

    A failure here means a surface drifted from the §3.5 / §3.4.2 spec.
    Update the offending surface; do NOT relax the assertion.
    """
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
    assert not failures, (
        f'F43 cross-surface parity broke for concept {concept!r}:\n'
        + '\n'.join('  - ' + f for f in failures)
        + '\nSee cognitive-memory-research-report.md §3.5 + §3.4.1 + §3.4.2.'
    )
