"""Tests for entity discovery from who/where fields in _resolve_entities.

Validates that person names from 'who' and location names from 'where' are
discovered via capitalized-name regex and linked even when the LLM omits
them from the entities list.

NER is NOT used in the extraction pipeline (only in retrieval).
"""

import datetime as dt
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.models import Entity, ProcessedFact
from memex_common.schemas import FactTypes


def _make_fact(
    text: str,
    who: str | None = None,
    where: str | None = None,
    entities: list[Entity] | None = None,
) -> ProcessedFact:
    return ProcessedFact(
        fact_text=text,
        fact_type=FactTypes.WORLD,
        embedding=[0.1] * 384,
        mentioned_at=dt.datetime.now(dt.timezone.utc),
        who=who,
        where=where,
        entities=entities or [],
    )


def _make_engine() -> ExtractionEngine:
    """Create an ExtractionEngine with mocked dependencies."""
    engine = ExtractionEngine.__new__(ExtractionEngine)
    engine.entity_resolver = AsyncMock()
    engine.entity_resolver.resolve_entities_batch = AsyncMock(return_value=[])
    engine.entity_resolver.link_units_to_entities_batch = AsyncMock()
    return engine


class TestWhoWhereDiscovery:
    """Test entity discovery from who/where fields using capitalized-name regex."""

    @pytest.mark.asyncio
    async def test_who_names_discovered(self):
        """Capitalized names in 'who' field are discovered and added as Person entities."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='The project was completed successfully.',
            who='Emily Chen (lead engineer)',
            entities=[],
        )

        session = AsyncMock()
        await engine._resolve_entities(session, [unit_id], [fact])

        engine.entity_resolver.resolve_entities_batch.assert_awaited_once()
        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        texts = [e['text'] for e in entities_data]
        assert any('Emily Chen' in t for t in texts), (
            f'Expected "Emily Chen" in entities, got: {texts}'
        )

        person_entities = [e for e in entities_data if e['entity_type'] == 'Person']
        assert len(person_entities) >= 1

    @pytest.mark.asyncio
    async def test_where_locations_discovered(self):
        """Capitalized names in 'where' field are discovered as Location entities."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='A meeting was held.',
            where='San Francisco, California',
            entities=[],
        )

        session = AsyncMock()
        await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        texts = [e['text'] for e in entities_data]
        assert any('San Francisco' in t for t in texts), (
            f'Expected "San Francisco" in entities, got: {texts}'
        )

        location_entities = [e for e in entities_data if e['entity_type'] == 'Location']
        assert len(location_entities) >= 1

    @pytest.mark.asyncio
    async def test_already_extracted_entities_not_duplicated(self):
        """Entities already in the entities list are not added again from who/where."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='Emily presented the results.',
            who='Emily Chen',
            entities=[Entity(text='Emily Chen', entity_type='Person')],
        )

        session = AsyncMock()
        await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        emily_entries = [e for e in entities_data if 'emily' in e['text'].lower()]
        assert len(emily_entries) == 1, (
            f'Expected exactly 1 Emily entry, got {len(emily_entries)}: {emily_entries}'
        )

    @pytest.mark.asyncio
    async def test_na_who_where_skipped(self):
        """N/A and None values in who/where are ignored."""
        engine = _make_engine()
        unit_id = str(uuid4())

        fact = _make_fact(
            text='Something happened.',
            who='N/A',
            where='None',
            entities=[],
        )

        session = AsyncMock()
        result = await engine._resolve_entities(session, [unit_id], [fact])

        assert result == set()

    @pytest.mark.asyncio
    async def test_capitalized_names_extracted_from_who(self):
        """Multiple capitalized names in 'who' are all extracted."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_ids = [str(uuid4()), str(uuid4())]
        engine.entity_resolver.resolve_entities_batch.return_value = resolved_ids

        fact = _make_fact(
            text='The project launched.',
            who='Emily Chen and Sarah Johnson',
            entities=[],
        )

        session = AsyncMock()
        await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        texts = {e['text'].lower() for e in entities_data}
        assert 'emily chen' in texts, f'Expected "Emily Chen" in {texts}'
        assert 'sarah johnson' in texts, f'Expected "Sarah Johnson" in {texts}'

        for e in entities_data:
            assert e['entity_type'] == 'Person'

    @pytest.mark.asyncio
    async def test_capitalized_locations_extracted_from_where(self):
        """Capitalized names in 'where' are extracted as Location."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_ids = [str(uuid4()), str(uuid4())]
        engine.entity_resolver.resolve_entities_batch.return_value = resolved_ids

        fact = _make_fact(
            text='The event happened.',
            where='San Francisco at the Moscone Center',
            entities=[],
        )

        session = AsyncMock()
        await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        texts = {e['text'].lower() for e in entities_data}
        assert 'san francisco' in texts, f'Expected "San Francisco" in {texts}'
        assert 'moscone center' in texts, f'Expected "Moscone Center" in {texts}'

        for e in entities_data:
            assert e['entity_type'] == 'Location'

    @pytest.mark.asyncio
    async def test_common_words_not_extracted(self):
        """Common filler words like 'The' are not extracted as entities."""
        engine = _make_engine()
        unit_id = str(uuid4())

        fact = _make_fact(
            text='Something.',
            who='The team and Emily',
            entities=[],
        )

        session = AsyncMock()
        await engine._resolve_entities(session, [unit_id], [fact])

        if engine.entity_resolver.resolve_entities_batch.call_args:
            entities_data = engine.entity_resolver.resolve_entities_batch.call_args[0][1]
            texts = {e['text'].lower() for e in entities_data}
            assert 'the' not in texts, f'"The" should not be extracted, got: {texts}'

    @pytest.mark.asyncio
    async def test_entity_type_from_llm_preserved(self):
        """Entity type provided by LLM extraction is used as-is (no NER override)."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='Google announced new features.',
            entities=[Entity(text='Google', entity_type='Organization')],
        )

        session = AsyncMock()
        await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        assert entities_data[0]['entity_type'] == 'Organization'

    @pytest.mark.asyncio
    async def test_entity_without_type_passes_none(self):
        """Entities without a type from LLM get None (no NER fallback)."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='Something about Acme Corp.',
            entities=[Entity(text='Acme Corp')],
        )

        session = AsyncMock()
        await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        assert entities_data[0]['entity_type'] is None
