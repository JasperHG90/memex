"""Tests for briefing cache + block formatting.

After 2026-05-14 (three-tier agent-surface architecture): the briefing
composes ``memex_common.agent_surface.compose_with_procedural()`` (Tier 1b
universal + V7 procedural-plane doctrine) plus a hermes-specific harness
block (Tier 2). Universal content lives in
``memex_common.agent_surface``; this file pins how Hermes assembles it
on top of agent-specific framing.

Regression fences for the universal content live in
``packages/common/tests/test_agent_surface.py``. Regression fences for
per-tool descriptions live in
``packages/common/tests/test_tool_descriptions.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from memex_hermes_plugin.memex.briefing import (
    BriefingCache,
    _HERMES_HARNESS,
    format_briefing_block,
)


def test_cache_returns_empty_on_timeout():
    cache = BriefingCache()
    api = Mock()

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return 'late'

    api.get_session_briefing = slow
    cache.start_fetch(api, vault_id=uuid4(), budget=2000, project_id='p')
    assert cache.get(timeout=0.1) == ''


def test_cache_returns_result():
    cache = BriefingCache()
    api = Mock()
    api.get_session_briefing = AsyncMock(return_value='# Briefing\nRecent work: X.')
    cache.start_fetch(api, vault_id=uuid4(), budget=2000, project_id='p')
    assert 'Briefing' in cache.get(timeout=5.0)


def test_cache_records_error():
    cache = BriefingCache()
    api = Mock()
    api.get_session_briefing = AsyncMock(side_effect=RuntimeError('boom'))
    cache.start_fetch(api, vault_id=uuid4(), budget=2000, project_id='p')
    cache.get(timeout=5.0)
    assert 'boom' in (cache.get_error() or '')


def test_cache_reset_clears_state():
    cache = BriefingCache()
    api = Mock()
    api.get_session_briefing = AsyncMock(return_value='hello')
    cache.start_fetch(api, vault_id=uuid4(), budget=2000, project_id='p')
    cache.get(timeout=5.0)
    cache.reset()
    assert cache.get(timeout=0.01) == ''


def test_format_block_with_vault_and_briefing():
    block = format_briefing_block(
        '# Recent activity\n- Did X',
        vault_id='my-vault',
        project_id='github.com/acme/x',
        session_note_key='hermes:session:2026-01-01T00:00:00.000Z',
        kv_instructions_if_no_vault=False,
    )
    assert 'Memex Memory' in block
    assert '`my-vault`' in block
    assert 'github.com/acme/x' in block
    assert 'hermes:session:2026-01-01T00:00:00.000Z' in block
    assert '# Recent activity' in block


def test_format_block_carries_universal_block_from_agent_surface():
    """The briefing must include the Tier 1b universal content. This is the
    primary architecture wire — universal content arrives via
    `compose_universal()`, not redeclared locally."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    # Header from agent_surface
    assert '## Memex — system instructions' in block
    # Load-bearing universal-content markers
    assert 'units=[{unit_id, verb, reason}]' in block
    assert 'unit_metadata.virtual' in block
    assert 'scope qualifier' in block


def test_format_block_carries_hermes_harness():
    """The Tier 2 hermes-specific framing must layer on top of the universal block."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    assert 'Hermes-specific framing' in block
    assert 'memex_add_note' in block


def test_format_block_without_vault_adds_kv_guidance():
    block = format_briefing_block(
        '',
        vault_id=None,
        project_id='p',
        session_note_key='hermes:session:abc',
        kv_instructions_if_no_vault=True,
    )
    assert 'No vault bound' in block
    assert 'project:p:vault' in block


def test_format_block_skips_briefing_section_when_empty():
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    # The `\n---\n` literal is the briefing-section separator added in
    # format_briefing_block only when the `briefing` argument is non-empty.
    assert '\n---\n' not in block


# ---------------------------------------------------------------------------
# Compression invariants: things that should NO LONGER be locally declared
# in the briefing module (they live in agent_surface).
# ---------------------------------------------------------------------------


def test_hermes_briefing_reexports_ssot_harness_by_identity() -> None:
    """Hermes' in-process briefing path must use the same harness object as
    the CLI bridge — drift across paths is the bug this SSOT prevents.

    Lives in hermes-plugin tests (not common tests) because importing
    ``memex_hermes_plugin.memex.briefing`` transitively imports
    ``agent.memory_provider``, a Hermes-only module stubbed by this
    package's conftest.
    """
    from memex_common.agent_harnesses import HERMES_HARNESS
    from memex_hermes_plugin.memex.briefing import _HERMES_HARNESS

    assert _HERMES_HARNESS is HERMES_HARNESS, (
        '`briefing._HERMES_HARNESS` is not the canonical '
        '`memex_common.agent_harnesses.HERMES_HARNESS` object — '
        'this means a local copy was re-introduced; replace with a re-export.'
    )


def test_briefing_does_not_locally_declare_universal_constants():
    """The briefing module must not re-declare universal-tier content
    locally. compose_universal() is the only source."""
    from memex_hermes_plugin.memex import briefing as br

    # The renamed constants from earlier compression (_CITATION_DISCIPLINE,
    # _AGENT_NUDGE, _ROUTING_GUIDE, _STORAGE_MODEL_PRIMER,
    # _RESOLUTION_FLOW_PRIMER) must not exist anymore; their content moved
    # to agent_surface.
    for name in (
        '_CITATION_DISCIPLINE',
        '_AGENT_NUDGE',
        '_ROUTING_GUIDE',
        '_STORAGE_MODEL_PRIMER',
        '_RESOLUTION_FLOW_PRIMER',
    ):
        assert not hasattr(br, name), (
            f'briefing.py still declares {name} locally. Universal content '
            'moved to memex_common.agent_surface (Tier 1b).'
        )


# ---------------------------------------------------------------------------
# Hermes-harness content (Tier 2 only).
# ---------------------------------------------------------------------------


def test_hermes_harness_carries_outcome_lexicon():
    """Hermes-specific harness: map user-signal phrases to record_outcome verbs.
    The universal block teaches the verb shape; this block teaches the lexicon."""
    assert 'helpful' in _HERMES_HARNESS
    assert 'not_helpful' in _HERMES_HARNESS
    assert 'that worked' in _HERMES_HARNESS.lower() or 'that fixed it' in _HERMES_HARNESS.lower()


def test_hermes_harness_carries_capture_cadence():
    """The capture nudge is hermes-specific (capture is generally agent-specific;
    Claude Code has its own capture nudge with author='claude-code')."""
    assert 'memex_add_note' in _HERMES_HARNESS
    assert '300 tokens' in _HERMES_HARNESS


def test_hermes_harness_within_budget():
    """Tier 2 hermes harness ≤1,600 chars / ≤400 tokens."""
    assert len(_HERMES_HARNESS) <= 1_600, (
        f'_HERMES_HARNESS is {len(_HERMES_HARNESS)} chars; cap is 1,600. '
        'Move agent-specific content here; universal content belongs in agent_surface.'
    )


# ---------------------------------------------------------------------------
# V7 procedural-plane doctrine in the briefing block.
# The briefing composes `compose_with_procedural()` (universal + procedural
# block) on top of the Hermes-specific harness. These tests pin that the
# procedural routing rules are visible to the agent — a regression that
# silently fell back to `compose_universal()` would break V7 write routing
# for every Hermes session.
# ---------------------------------------------------------------------------


def test_format_block_carries_v7_procedural_doctrine():
    """The V7 procedural-plane doctrine MUST appear in the briefing
    block. Without it, a Hermes agent has no doctrine for routing
    ``"this is how to do X"`` write intents to the procedural plane —
    the eval-driven failure mode is that the agent falls back to
    ``memex_add_note`` (note plane) or ``memex_kv_put`` (KV plane),
    both of which silently route the wrong way.
    """
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    # The doctrine block identity marker.
    assert '## Procedural plane' in block
    # The three procedural kinds — every kind routes through the
    # procedural plane, not the note/KV plane.
    assert '`case`' in block
    assert '`procedure`' in block
    assert '`strategy`' in block
    # Identity-anchor rule (UNIQUE on (kind, scope, verb, context)).
    assert '(kind, scope, verb, context)' in block
    # Tool-name markers — at least one of the 8 memex_procedural_* tools
    # must be visible so the agent knows the routing surface exists.
    assert 'memex_procedural_create' in block
    # Briefing-cards pin-chain probe — the agent can request procedural
    # cards for the active session.
    assert 'memex_procedural_briefing_cards' in block


def test_format_block_uses_compose_with_procedural_not_universal():
    """Defence-in-depth trip-wire: a regression that swaps
    ``compose_with_procedural()`` back to ``compose_universal()``
    in ``briefing.py`` would still pass the universal-block presence
    tests but silently drop the V7 doctrine. This test pins the
    procedural block ITSELF (which is not in the universal block) as
    a positive marker — if the procedural heading is absent, the
    swap has happened.
    """
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    # The V7 block lives BELOW the universal block in
    # ``compose_with_procedural()``. It is NOT in ``compose_universal()``.
    # If the briefing falls back to the universal-only composition,
    # this heading vanishes.
    assert '## Procedural plane' in block


def test_format_block_carries_procedural_block_after_universal():
    """The composition order is universal → procedural → harness.
    A regression that puts the procedural block AFTER the Hermes
    harness would still pass the presence test above but break the
    cacheable-prompt-prefix invariant — the harness is dynamic-ish
    (per-vault state in a real flow) and the universal + procedural
    blocks are the cacheable prefix.

    We pin the order by checking the procedural heading's offset
    relative to the universal block's footer marker.
    """
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    # The universal block ends with the CRITICAL_FOOTER section; the
    # procedural block lives BELOW it. Find both headings and check
    # the relative order.
    universal_footer = '## Critical reminders'
    procedural_heading = '## Procedural plane'
    assert universal_footer in block, 'universal block missing — pre-test setup error'
    assert procedural_heading in block
    assert block.index(universal_footer) < block.index(procedural_heading), (
        'Procedural block must come AFTER the universal block; the '
        'composition order in briefing.py is reversed.'
    )
