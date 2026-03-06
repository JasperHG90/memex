"""Tests for entity discovery from who/where fields in _resolve_entities.

Validates that person names from 'who' and location names from 'where' are
discovered and linked even when the LLM omits them from the entities list.
"""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch
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


class TestNerBasedDiscovery:
    """Test entity discovery when NER model is available."""

    @pytest.mark.asyncio
    async def test_who_names_discovered_via_ner(self):
        """Names in 'who' field are discovered via NER and added as Person entities."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='The project was completed successfully.',
            who='Emily Chen (lead engineer)',
            entities=[],  # LLM missed the entity
        )

        # NER finds "Emily Chen" in the who field
        mock_ner = MagicMock()
        mock_ner.predict.side_effect = lambda text: (
            [{'word': 'Emily Chen', 'type': 'PER'}] if 'Emily' in text else []
        )

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            new_callable=AsyncMock,
            return_value=mock_ner,
        ):
            session = AsyncMock()
            await engine._resolve_entities(session, [unit_id], [fact])

        # Verify entity was resolved
        engine.entity_resolver.resolve_entities_batch.assert_awaited_once()
        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        # Should have discovered "emily chen" from who field
        texts = [e['text'] for e in entities_data]
        assert any('emily chen' in t.lower() for t in texts), (
            f'Expected "Emily Chen" in entities, got: {texts}'
        )

        # Verify entity type is Person
        person_entities = [e for e in entities_data if e['entity_type'] == 'Person']
        assert len(person_entities) >= 1

    @pytest.mark.asyncio
    async def test_where_locations_discovered_via_ner(self):
        """Locations in 'where' field are discovered via NER."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='A meeting was held.',
            where='San Francisco, California',
            entities=[],
        )

        mock_ner = MagicMock()
        mock_ner.predict.side_effect = lambda text: (
            [{'word': 'San Francisco', 'type': 'LOC'}] if 'San Francisco' in text else []
        )

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            new_callable=AsyncMock,
            return_value=mock_ner,
        ):
            session = AsyncMock()
            await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        texts = [e['text'] for e in entities_data]
        assert any('san francisco' in t.lower() for t in texts), (
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

        mock_ner = MagicMock()
        mock_ner.predict.return_value = [{'word': 'Emily Chen', 'type': 'PER'}]

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            new_callable=AsyncMock,
            return_value=mock_ner,
        ):
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

        mock_ner = MagicMock()
        mock_ner.predict.return_value = []

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            new_callable=AsyncMock,
            return_value=mock_ner,
        ):
            session = AsyncMock()
            result = await engine._resolve_entities(session, [unit_id], [fact])

        assert result == set()


class TestRegexFallback:
    """Test entity discovery when NER model is unavailable."""

    @pytest.mark.asyncio
    async def test_capitalized_names_extracted_from_who(self):
        """When NER is unavailable, capitalized names in 'who' are extracted."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='The project launched.',
            who='Emily Chen and Sarah Johnson',
            entities=[],
        )

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            side_effect=ImportError('no ner model'),
        ):
            session = AsyncMock()
            await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        texts = {e['text'].lower() for e in entities_data}
        assert 'emily chen' in texts, f'Expected "Emily Chen" in {texts}'
        assert 'sarah johnson' in texts, f'Expected "Sarah Johnson" in {texts}'

        # All should be typed as Person (from who field default)
        for e in entities_data:
            assert e['entity_type'] == 'Person'

    @pytest.mark.asyncio
    async def test_capitalized_locations_extracted_from_where(self):
        """When NER is unavailable, capitalized names in 'where' are extracted as Location."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='The event happened.',
            where='San Francisco at the Moscone Center',
            entities=[],
        )

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            side_effect=ImportError('no ner model'),
        ):
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

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            side_effect=ImportError('no ner model'),
        ):
            session = AsyncMock()
            await engine._resolve_entities(session, [unit_id], [fact])

        if engine.entity_resolver.resolve_entities_batch.call_args:
            entities_data = engine.entity_resolver.resolve_entities_batch.call_args[0][1]
            texts = {e['text'].lower() for e in entities_data}
            assert 'the' not in texts, f'"The" should not be extracted, got: {texts}'

    @pytest.mark.asyncio
    async def test_regex_fallback_skipped_when_ner_finds_entities(self):
        """Regex fallback should NOT run when NER already found entities in who/where."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_id = str(uuid4())
        engine.entity_resolver.resolve_entities_batch.return_value = [resolved_id]

        fact = _make_fact(
            text='Project update.',
            who='Emily Chen (lead engineer)',
            entities=[],
        )

        mock_ner = MagicMock()
        mock_ner.predict.side_effect = lambda text: (
            [{'word': 'Emily Chen', 'type': 'PER'}] if 'Emily' in text else []
        )

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            new_callable=AsyncMock,
            return_value=mock_ner,
        ):
            session = AsyncMock()
            await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        entities_data = call_args[0][1]

        # Should only have "emily chen" from NER, not duplicated by regex
        emily_entries = [e for e in entities_data if 'emily' in e['text'].lower()]
        assert len(emily_entries) == 1
