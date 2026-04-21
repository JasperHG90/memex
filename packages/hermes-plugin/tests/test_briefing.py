"""Tests for briefing cache + block formatting."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from memex_hermes_plugin.memex.briefing import BriefingCache, format_briefing_block


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
    # Block up to 5s for the single-call coroutine to finish.
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


def test_format_block_contains_routing_guidance():
    """Routing advice (parallel for content lookup, survey for broad, etc.) lives here,
    not in per-tool descriptions."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    assert 'How to use Memex tools' in block
    assert 'memex_survey' in block
    assert 'memex_list_entities' in block


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
    assert '---' not in block
