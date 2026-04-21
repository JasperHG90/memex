"""Tests for the two-layer prefetch cache."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from memex_common.schemas import BlockSummaryDTO, MemoryUnitDTO, NoteSearchResult

from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.prefetch import PrefetchCache


def _fact(text: str) -> MemoryUnitDTO:
    return MemoryUnitDTO(
        id=uuid4(),
        note_id=uuid4(),
        text=text,
        fact_type='world',
        status='active',
    )


def _note_result(title: str) -> NoteSearchResult:
    return NoteSearchResult(
        note_id=uuid4(),
        metadata={'name': title},
        summaries=[BlockSummaryDTO(topic='preview', key_points=[])],
    )


def test_consume_returns_empty_when_no_data():
    cache = PrefetchCache()
    assert cache.consume(timeout=0.1) == ''


def test_formats_both_sections_when_results_present():
    cache = PrefetchCache()
    config = HermesMemexConfig()
    api = Mock()
    api.search = AsyncMock(return_value=[_fact('foo'), _fact('bar')])
    api.search_notes = AsyncMock(return_value=[_note_result('Doc A')])
    cache.queue('q', api=api, config=config, vault_id=uuid4())
    text = cache.consume(timeout=5.0)
    assert 'Memex — Facts' in text
    assert '- foo' in text
    assert 'Memex — Related Notes' in text
    assert 'Doc A' in text


def test_consume_clears_cache():
    cache = PrefetchCache()
    config = HermesMemexConfig()
    api = Mock()
    api.search = AsyncMock(return_value=[_fact('foo')])
    api.search_notes = AsyncMock(return_value=[])
    cache.queue('q', api=api, config=config, vault_id=uuid4())
    first = cache.consume(timeout=5.0)
    assert 'foo' in first
    # second consume: nothing new queued → empty.
    assert cache.consume(timeout=0.1) == ''


def test_facts_only_when_notes_empty():
    cache = PrefetchCache()
    config = HermesMemexConfig()
    api = Mock()
    api.search = AsyncMock(return_value=[_fact('only')])
    api.search_notes = AsyncMock(return_value=[])
    cache.queue('q', api=api, config=config, vault_id=uuid4())
    text = cache.consume(timeout=5.0)
    assert 'Memex — Facts' in text
    assert 'Related Notes' not in text


def test_slow_fetch_respects_timeout():
    cache = PrefetchCache()
    config = HermesMemexConfig()
    api = Mock()

    async def slow_search(*args, **kwargs):
        await asyncio.sleep(5)
        return []

    api.search = slow_search
    api.search_notes = slow_search
    cache.queue('q', api=api, config=config, vault_id=uuid4())
    assert cache.consume(timeout=0.1) == ''
