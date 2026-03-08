"""Tests for fragmented entity extraction in the _resolve_entities pipeline.

Reproduces the issue where NER sub-word tokenization produces fragments
(e.g. "Rit" instead of "Rituals") that leak into entities_data.
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
    engine = ExtractionEngine.__new__(ExtractionEngine)
    engine.entity_resolver = AsyncMock()
    engine.entity_resolver.resolve_entities_batch = AsyncMock(return_value=[])
    engine.entity_resolver.link_units_to_entities_batch = AsyncMock()
    return engine


def _make_ner_mock(results_by_text: dict[str, list[dict]]) -> MagicMock:
    """Create a mock NER model that returns fragment results based on input text.

    Args:
        results_by_text: mapping of substring -> NER results to return when
            that substring appears in the input.
    """
    mock = MagicMock()

    def predict(text):
        for key, results in results_by_text.items():
            if key in text:
                return results
        return []

    mock.predict.side_effect = predict
    return mock


class TestNerFragmentLeak:
    """Reproduce: NER returns sub-word fragments that leak into entities_data."""

    @pytest.mark.asyncio
    async def test_ner_subword_fragment_leaks_via_who_field(self):
        """When NER returns a fragment like 'Rit' (from 'Rituals'), it should
        NOT appear as a standalone entity in entities_data."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_ids = [str(uuid4())]
        engine.entity_resolver.resolve_entities_batch.return_value = resolved_ids

        fact = _make_fact(
            text='The team discussed Rituals for the sprint.',
            who='Rituals team',
            entities=[],  # LLM missed the entity
        )

        # Simulate NER returning a fragment: the model labeled only the first
        # sub-token of "Rituals" as B-ORG, so predict() returns "Rit"
        mock_ner = _make_ner_mock(
            {
                'Rituals': [{'word': 'Rit', 'type': 'ORG', 'start': 0, 'end': 3, 'score': 0.8}],
            }
        )

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            new_callable=AsyncMock,
            return_value=mock_ner,
        ):
            session = AsyncMock()
            await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        if call_args is None:
            # No entities resolved at all — that's acceptable
            return

        entities_data = call_args[0][1]
        texts = [e['text'] for e in entities_data]

        # "Rit" should NOT appear as a standalone entity
        assert 'rit' not in texts, f'Fragment "rit" leaked into entities_data: {texts}'

    @pytest.mark.asyncio
    async def test_ner_single_letter_fragment_leaks(self):
        """Single-letter NER fragments like 's' should not become entities."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_ids = [str(uuid4())]
        engine.entity_resolver.resolve_entities_batch.return_value = resolved_ids

        fact = _make_fact(
            text='The Sys Layer handles authentication.',
            who='Sys Layer team',
            entities=[],
        )

        # NER returns single-letter fragment from "Sys"
        mock_ner = _make_ner_mock(
            {
                'Sys': [{'word': 'S', 'type': 'ORG', 'start': 0, 'end': 1, 'score': 0.6}],
            }
        )

        with patch(
            'memex_core.memory.models.ner.get_ner_model',
            new_callable=AsyncMock,
            return_value=mock_ner,
        ):
            session = AsyncMock()
            await engine._resolve_entities(session, [unit_id], [fact])

        call_args = engine.entity_resolver.resolve_entities_batch.call_args
        if call_args is None:
            return

        entities_data = call_args[0][1]
        texts = [e['text'] for e in entities_data]

        assert 's' not in texts, f'Single-letter fragment "s" leaked into entities_data: {texts}'

    @pytest.mark.asyncio
    async def test_ner_fragment_no_substring_match_across_word_boundary(self):
        """Fragment 'api' from 'Apigee' should not match inside 'Apigee' via
        substring. Word boundary matching should prevent 'api' matching
        as part of 'Apigee'."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_ids = [str(uuid4())]
        engine.entity_resolver.resolve_entities_batch.return_value = resolved_ids

        fact = _make_fact(
            text='Apigee handles the gateway.',
            who='Apigee team',
            entities=[Entity(text='Apigee', entity_type='Technology')],
        )

        # NER returns fragment "api" from partial labeling of "Apigee"
        mock_ner = _make_ner_mock(
            {
                'Apigee': [
                    {'word': 'Api', 'type': 'ORG', 'start': 0, 'end': 3, 'score': 0.7},
                ],
            }
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

        # "Apigee" should be there (from fact.entities)
        assert 'Apigee' in texts

        # "api" should NOT appear — it's a fragment of "Apigee", not a word
        fragment_entries = [t for t in texts if t == 'api']
        assert len(fragment_entries) == 0, f'Fragment "api" leaked as standalone entity: {texts}'

    @pytest.mark.asyncio
    async def test_no_fragments_when_ner_unavailable(self):
        """When NER model is unavailable, no fragments can leak — only regex
        fallback runs, which extracts capitalized words."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_ids = [str(uuid4()), str(uuid4())]
        engine.entity_resolver.resolve_entities_batch.return_value = resolved_ids

        fact = _make_fact(
            text='Rituals and Sys Layer are important systems.',
            who='Rituals team, Sys Layer team',
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
        texts = [e['text'] for e in entities_data]

        # Regex fallback should extract full capitalized words, not fragments
        assert all(len(t) > 1 for t in texts), f'Single-char entities found: {texts}'
        assert 'rit' not in [t.lower() for t in texts], f'Fragment found in: {texts}'

    @pytest.mark.asyncio
    async def test_llm_entities_are_not_fragmented(self):
        """Entities already on fact.entities (from LLM) should pass through
        intact regardless of NER results."""
        engine = _make_engine()
        unit_id = str(uuid4())
        resolved_ids = [str(uuid4()), str(uuid4()), str(uuid4())]
        engine.entity_resolver.resolve_entities_batch.return_value = resolved_ids

        fact = _make_fact(
            text='Rituals team uses Apigee for the EDP.',
            entities=[
                Entity(text='Rituals', entity_type='Organization'),
                Entity(text='Apigee', entity_type='Technology'),
                Entity(text='EDP', entity_type='Technology'),
            ],
        )

        # NER returns fragments — but LLM entities should be unaffected
        mock_ner = _make_ner_mock(
            {
                'Rituals': [{'word': 'Rit', 'type': 'ORG', 'start': 0, 'end': 3, 'score': 0.8}],
            }
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

        # All three LLM entities should be present intact
        assert 'Rituals' in texts
        assert 'Apigee' in texts
        assert 'EDP' in texts

        # No fragments
        assert 'Rit' not in texts and 'rit' not in texts
