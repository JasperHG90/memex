"""Token-budget enforcement for the Hermes Tier 2 surface.

The three-tier agent-surface architecture caps the Hermes-specific framing
at ≤1,600 chars / ≤400 tokens. Everything beyond that — storage model,
retrieval routing, 5-step flow, KV scope-qualifier rule — is universal
content and lives in ``memex_common.agent_surface`` (Tier 1b).

This test pins:
1. ``_HERMES_HARNESS`` (the Tier 2 hermes-only string) is within budget.
2. The composed briefing block carries the universal block + harness +
   dynamic state, and its total size lives within the hermes session-
   prompt budget (system-prompt addition + per-vault briefing).

If the hermes harness needs to grow, that's a signal to ask whether the
new content is hermes-specific or universal — universal additions should
go to ``agent_surface``, where every Memex-aware agent benefits.
"""

from __future__ import annotations

from memex_hermes_plugin.memex.briefing import _HERMES_HARNESS, format_briefing_block


_HERMES_HARNESS_CHAR_CAP = 1_600
_HERMES_HARNESS_TOKEN_CAP = 400

# Total briefing block (universal + hermes harness + dynamic state +
# server-side briefing string) lives under the system-prompt 1,500–6,000-token
# window from indiehackers' empirical Claude Code analysis. The cap below
# captures the static prefix; per-vault dynamic content adds variable size.
_TOTAL_STATIC_PREFIX_CHAR_CAP = 9_400


def _approx_tokens(text: str) -> int:
    """~3.5 chars/token — empirical against tiktoken cl100k_base on this repo's markdown."""
    return (len(text) * 2 + 6) // 7


def test_hermes_harness_within_char_budget() -> None:
    assert len(_HERMES_HARNESS) <= _HERMES_HARNESS_CHAR_CAP, (
        f'_HERMES_HARNESS is {len(_HERMES_HARNESS)} chars (~'
        f'{_approx_tokens(_HERMES_HARNESS)} tok); cap is {_HERMES_HARNESS_CHAR_CAP}. '
        'Move agent-specific content here; universal content belongs in '
        '`memex_common.agent_surface`.'
    )


def test_hermes_harness_within_token_budget() -> None:
    approx = _approx_tokens(_HERMES_HARNESS)
    assert approx <= _HERMES_HARNESS_TOKEN_CAP, (
        f'_HERMES_HARNESS is ~{approx} tokens; cap is {_HERMES_HARNESS_TOKEN_CAP}.'
    )


def test_format_briefing_block_static_prefix_within_budget() -> None:
    """Static prefix (universal + harness + minimal dynamic stubs) caps the
    system-prompt addition cost per session start."""
    block = format_briefing_block(
        briefing='',  # no per-vault dynamic content
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    assert len(block) <= _TOTAL_STATIC_PREFIX_CHAR_CAP, (
        f'Hermes briefing block static prefix is {len(block)} chars '
        f'(~{_approx_tokens(block)} tok); cap is {_TOTAL_STATIC_PREFIX_CHAR_CAP}. '
        'Reduce hermes harness or universal block; the static prefix sits '
        'in the system-prompt cacheable region.'
    )
