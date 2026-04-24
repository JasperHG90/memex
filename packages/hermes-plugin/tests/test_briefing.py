"""Tests for briefing cache + block formatting."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from memex_hermes_plugin.memex.briefing import (
    BriefingCache,
    _ROUTING_GUIDE,
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


# --- Routing-guide bullets (AC-087..AC-092) ---


def test_routing_guide_explains_vault_scoping():
    """AC-087: Vault scoping bullet must name vault_ids and warn against tags."""
    assert '**Vault scoping**' in _ROUTING_GUIDE
    assert 'vault_ids' in _ROUTING_GUIDE
    assert 'Do NOT use `tags`' in _ROUTING_GUIDE


def test_routing_guide_documents_vault_discovery():
    """AC-088: Vault discovery bullet names list_vaults and get_vault_summary."""
    assert '**Vault discovery**' in _ROUTING_GUIDE
    assert 'memex_list_vaults' in _ROUTING_GUIDE
    assert 'memex_get_vault_summary' in _ROUTING_GUIDE


def test_routing_guide_title_bullet_uses_find_note():
    """AC-089: Title known bullet must reference memex_find_note, not memex_retrieve_notes."""
    assert '**Title known**' in _ROUTING_GUIDE
    assert 'memex_find_note' in _ROUTING_GUIDE
    # Title-known bullet must no longer point at retrieve_notes for title lookups.
    title_line_end = _ROUTING_GUIDE.index('\n', _ROUTING_GUIDE.index('**Title known**'))
    title_bullet = _ROUTING_GUIDE[_ROUTING_GUIDE.index('**Title known**') : title_line_end + 120]
    assert 'memex_retrieve_notes' not in title_bullet


def test_routing_guide_documents_kv_store():
    """AC-090: KV store bullet names all 4 KV tools and 4 namespace prefixes."""
    assert '**KV store**' in _ROUTING_GUIDE
    for tool in ('memex_kv_write', 'memex_kv_get', 'memex_kv_search', 'memex_kv_list'):
        assert tool in _ROUTING_GUIDE
    for prefix in ('`global:`', '`user:`', '`project:<id>:`', '`app:<id>:`'):
        assert prefix in _ROUTING_GUIDE
    assert 'CLI-only' in _ROUTING_GUIDE


def test_routing_guide_documents_lineage():
    """AC-091: Lineage bullet names get_memory_links and get_lineage."""
    assert '**Lineage / relationships**' in _ROUTING_GUIDE
    assert 'memex_get_memory_links' in _ROUTING_GUIDE
    assert 'memex_get_lineage' in _ROUTING_GUIDE
    # Mention of typed-link kinds and provenance chain.
    assert 'temporal' in _ROUTING_GUIDE
    assert 'causal' in _ROUTING_GUIDE
    assert 'mental_model' in _ROUTING_GUIDE


def test_routing_guide_documents_batch_fetch():
    """AC-092: Batch fetch bullet names get_entities and get_memory_units."""
    assert '**Batch fetch**' in _ROUTING_GUIDE
    assert 'memex_get_entities' in _ROUTING_GUIDE
    assert 'memex_get_memory_units' in _ROUTING_GUIDE


def test_routing_guide_bullets_render_in_formatted_block():
    """Guide must flow through format_briefing_block end-to-end."""
    block = format_briefing_block(
        '',
        vault_id='v',
        project_id='p',
        session_note_key='k',
        kv_instructions_if_no_vault=False,
    )
    for marker in (
        '**Vault scoping**',
        '**Vault discovery**',
        '**Batch fetch**',
        '**Lineage / relationships**',
        '**KV store**',
        'memex_find_note',
    ):
        assert marker in block
