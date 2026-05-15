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

_TIER_2_CHAR_CAP = 1_600


def test_hermes_harness_within_budget() -> None:
    assert len(HERMES_HARNESS) <= _TIER_2_CHAR_CAP, (
        f'HERMES_HARNESS is {len(HERMES_HARNESS)} chars; cap is {_TIER_2_CHAR_CAP}.'
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


def test_cli_agent_surface_uses_ssot_harnesses_by_identity() -> None:
    """The CLI bridge composes from the SSOT harness objects directly."""
    from memex_cli import agent_surface as cli_surface

    # The module should reference HERMES_HARNESS / CLAUDE_CODE_HARNESS as
    # imported names — accessing them via the module namespace yields the
    # same object.
    assert cli_surface.HERMES_HARNESS is HERMES_HARNESS
    assert cli_surface.CLAUDE_CODE_HARNESS is CLAUDE_CODE_HARNESS
