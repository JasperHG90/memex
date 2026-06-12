"""Tests for ``memex_common.agent_harnesses`` — the Tier 2 per-agent harness SSOT.

The harness strings used to be defined separately in Hermes' briefing and
in the CLI ``agent-surface`` emitter, with no guarantee they stayed in
sync. This module is the single source of truth; both consumers import
the same object by identity.

Tests pin:
1. Each harness exists and is within its Tier 2 budget (≤1,600 chars).
2. Load-bearing per-agent keywords are present.
3. Each consumer re-exports the SSOT object by identity, not by copy.
"""

from __future__ import annotations

from memex_common.agent_harnesses import CLAUDE_CODE_HARNESS, HERMES_HARNESS

_TIER_2_CHAR_CAP = 5_300
# Bumped 5,600 → 5,900 when the write_routing constraint gained two
# procedure entries: global default (`procedure:<verb>:<context>`) and
# the project-scoped variant gated on an explicit cue
# (`project:<id>:procedure:*`). Without the explicit ASK directive the
# agent silently project-scopes by cwd, producing keys the user didn't
# ask for. The +214 chars buys the routing rule + ambiguity escape.
# Lowered 5,900 → 5,500 after an LLM-consumption pass tightened every
# constraint to imperative one-liners (filler/meta cut, examples trimmed)
# while keeping every routing rule, pinned tool name, and the
# memex_case_submit workflow model — harness fell 5,839 → 5,277. Ratchet
# the cap down with the content so cap_is_tight keeps surfacing growth.
# Hermes has a tighter natural ceiling — its harness only needs the
# outcome lexicon + capture cadence. Pin its cap separately so the CC
# harness's expansion doesn't relax the Hermes budget.
_HERMES_CHAR_CAP = 1_600


def test_hermes_harness_within_budget() -> None:
    assert len(HERMES_HARNESS) <= _HERMES_CHAR_CAP, (
        f'HERMES_HARNESS is {len(HERMES_HARNESS)} chars; cap is {_HERMES_CHAR_CAP}.'
    )


def test_claude_code_harness_within_budget() -> None:
    assert len(CLAUDE_CODE_HARNESS) <= _TIER_2_CHAR_CAP, (
        f'CLAUDE_CODE_HARNESS is {len(CLAUDE_CODE_HARNESS)} chars; cap is {_TIER_2_CHAR_CAP}.'
    )


def test_hermes_harness_carries_outcome_lexicon() -> None:
    assert 'helpful' in HERMES_HARNESS
    assert 'not_helpful' in HERMES_HARNESS
    assert 'that worked' in HERMES_HARNESS or 'that fixed it' in HERMES_HARNESS


def test_claude_code_harness_carries_capture_cadence_and_slash_commands() -> None:
    assert 'memex_add_note' in CLAUDE_CODE_HARNESS
    assert 'author="claude-code"' in CLAUDE_CODE_HARNESS
    assert '/remember' in CLAUDE_CODE_HARNESS
    assert '/recall' in CLAUDE_CODE_HARNESS


def test_claude_code_harness_carries_answer_from_briefing_rule() -> None:
    """The answer-from-briefing rule must not silently disappear under future trims."""
    assert 'answer_from_briefing' in CLAUDE_CODE_HARNESS
    assert 'memex_get_vault_summary' in CLAUDE_CODE_HARNESS
    assert 'memex_kv_list' in CLAUDE_CODE_HARNESS


def test_claude_code_harness_cap_is_tight() -> None:
    """The cap is intentionally close to the harness length — bump cap if this fails,
    don't pad the harness. Surfaces ratchet pressure on every meaningful trim/add."""
    margin = _TIER_2_CHAR_CAP - len(CLAUDE_CODE_HARNESS)
    assert margin < 300, (
        f'cap is loose ({margin} chars of slack); tighten _TIER_2_CHAR_CAP or document why.'
    )


def test_cli_agent_surface_uses_ssot_harnesses_by_identity() -> None:
    """The CLI bridge composes from the SSOT harness objects directly."""
    from memex_cli import agent_surface as cli_surface

    # The module should reference HERMES_HARNESS / CLAUDE_CODE_HARNESS as
    # imported names — accessing them via the module namespace yields the
    # same object.
    assert cli_surface.HERMES_HARNESS is HERMES_HARNESS
    assert cli_surface.CLAUDE_CODE_HARNESS is CLAUDE_CODE_HARNESS
