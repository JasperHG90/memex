"""Agent-surface SSOT discipline tests.

Pins token budgets, content presence, and determinism for the Tier 1b
universal system-prompt content exported from
``memex_common.agent_surface``.

See ``CLAUDE.md`` §"Agent-surface architecture" for the three-tier model.
"""

from __future__ import annotations

import pytest

from memex_common import agent_surface as ags


# ---------------------------------------------------------------------------
# Budget — keep the universal block compact. Empirical target ~1,000 tokens.
# Hard cap set higher than current size to allow modest growth without
# slowing the test loop on every PR; a sustained breach should prompt a
# review rather than auto-pass.
# ---------------------------------------------------------------------------


_UNIVERSAL_CHAR_CAP = 5_500  # ~1,571 tokens at 3.5 chars/token (empirical cl100k)


def _approx_tokens(text: str) -> int:
    """~3.5 chars/token — empirical against tiktoken cl100k_base on this repo's markdown."""
    return (len(text) * 2 + 6) // 7


def test_compose_universal_within_budget() -> None:
    out = ags.compose_universal()
    assert len(out) <= _UNIVERSAL_CHAR_CAP, (
        f'compose_universal() is {len(out)} chars (~{_approx_tokens(out)} tokens), '
        f'exceeding cap {_UNIVERSAL_CHAR_CAP}. Either trim a section or '
        'lift the cap with rationale.'
    )


# ---------------------------------------------------------------------------
# Determinism — load-bearing for prompt-prefix cache hits.
# ---------------------------------------------------------------------------


def test_compose_universal_is_deterministic() -> None:
    """Calling compose_universal() twice must return byte-identical output.

    Per dbreunig's Claude Code cache-boundary analysis, the cacheable
    prompt prefix must produce identical bytes across turns/sessions —
    a single non-deterministic byte (timestamp, uuid, env probe) breaks
    every downstream cache hit.
    """
    a = ags.compose_universal()
    b = ags.compose_universal()
    assert a == b


# ---------------------------------------------------------------------------
# Content presence — load-bearing keywords that the agent must internalise.
# These are the constraints whose absence has historically caused eval
# regressions; pinning them here means an accidental trim fails CI.
# ---------------------------------------------------------------------------


_REQUIRED_KEYWORDS: tuple[str, ...] = (
    # V11 record_outcome contract — must be visible to every agent.
    'units=[{unit_id, verb, reason}]',
    'success=True',  # the bare-success rejection
    '400',
    # Virtual unit invariant.
    'unit_metadata.virtual',
    '404',
    # KV namespace prefixes.
    'user:',
    'project:<id>:',
    'global:',
    'app:<app-id>:',
    'procedure:<verb>:<context-tag>',
    # KV scope-qualifier rule.
    'scope qualifier',
    # 5-step flow anchors.
    'Disambiguate',
    'top_k',
    # Citation discipline.
    'Cite',
    # Retrieval routing tool names.
    'memex_find_note',
    'memex_memory_search',
    'memex_note_search',
    'memex_get_vault_summary',
    'memex_survey',
    # Resolution-flow verbs.
    'memex_record_outcome',
    'memex_memory_deprioritize',
)


@pytest.mark.parametrize('kw', _REQUIRED_KEYWORDS)
def test_compose_universal_carries_required_keyword(kw: str) -> None:
    out = ags.compose_universal()
    assert kw in out, (
        f'compose_universal() is missing required keyword {kw!r}. '
        'Either restore the section that carried it, or update '
        'this test if the constraint was deliberately dropped.'
    )


# ---------------------------------------------------------------------------
# Section constants — exist + non-empty.
# ---------------------------------------------------------------------------


_SECTION_CONSTANTS = (
    'CRITICAL_HEADER',
    'STORAGE_MODEL',
    'RETRIEVAL_ROUTING',
    'RESOLUTION_FLOW',
    'AXES',
    'HISTORICAL_ROUTING',
    'VIRTUAL_UNIT',
    'KV_NAMESPACE',
    'CITATIONS',
    'CRITICAL_FOOTER',
)


@pytest.mark.parametrize('name', _SECTION_CONSTANTS)
def test_section_constant_exists_and_non_empty(name: str) -> None:
    val = getattr(ags, name)
    assert isinstance(val, str)
    assert val.strip(), f'{name} is empty'


def test_layer_routing_primer_still_exported() -> None:
    """The 4-layer routing primer is kept as a standalone export even
    though ``compose_universal()`` does not include it by default —
    agents that want the 4-layer table can append it explicitly."""
    assert ags.LAYER_ROUTING_PRIMER_TABLE
    assert ags.LAYER_ROUTING_PRIMER_PROSE
    assert ags.LAYER_ROUTING_PRIMER_FRAGMENT


# ---------------------------------------------------------------------------
# U-shaped composition — header AND footer carry the same 4 load-bearing
# constraints (primacy + recency). This makes the model see them at both
# ends, where attention is strongest.
# ---------------------------------------------------------------------------


def test_header_and_footer_both_mention_record_outcome_shape() -> None:
    assert 'units=' in ags.CRITICAL_HEADER
    assert 'units=' in ags.CRITICAL_FOOTER


def test_header_and_footer_both_mention_virtual_units() -> None:
    assert 'virtual' in ags.CRITICAL_HEADER
    assert 'virtual' in ags.CRITICAL_FOOTER


def test_header_and_footer_both_mention_kv_scope_rule() -> None:
    assert 'scope qualifier' in ags.CRITICAL_HEADER
    assert 'scope qualifier' in ags.CRITICAL_FOOTER


def test_header_and_footer_both_mention_citations() -> None:
    assert 'Cite' in ags.CRITICAL_HEADER or 'cite' in ags.CRITICAL_HEADER.lower()
    assert 'Cite' in ags.CRITICAL_FOOTER or 'cite' in ags.CRITICAL_FOOTER.lower()
