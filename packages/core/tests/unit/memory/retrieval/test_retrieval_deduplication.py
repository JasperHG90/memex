from uuid import uuid4
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_common.types import FactTypes
from memex_core.memory.sql_models import MemoryUnit


@pytest.fixture
def mock_retrieval_engine():
    # We can pass None/MagicMock for dependencies as we are testing a pure logic method
    return RetrievalEngine(embedder=MagicMock(), reranker=MagicMock())


def test_deduplicate_and_cite_logic(mock_retrieval_engine):
    """
    Test that _deduplicate_and_cite collapses a Fact into an Observation
    if the Fact is listed as evidence for that Observation.
    """
    # 1. Setup Data
    fact_id = uuid4()
    fact_text = 'The user loves spicy curry.'
    fact_date = datetime(2024, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

    fact_unit = MemoryUnit(
        id=fact_id,
        text=fact_text,
        fact_type=FactTypes.WORLD,
        event_date=fact_date,
        note_id=uuid4(),
        embedding=[0.1] * 384,
    )

    obs_id = uuid4()
    obs_unit = MemoryUnit(
        id=obs_id,
        text='[User Preference] Diet: The user enjoys spicy food.',
        fact_type='observation',
        event_date=datetime(2024, 1, 25, tzinfo=timezone.utc),
        note_id=uuid4(),
        embedding=[0.2] * 384,
        unit_metadata={
            'observation': True,
            # This links the observation to the fact
            'evidence_ids': [str(fact_id)],
        },
    )

    # 2. Simulate Retrieval Result containing BOTH
    results = [obs_unit, fact_unit]

    # 3. Apply Deduplication
    deduplicated = mock_retrieval_engine._deduplicate_and_cite(results)

    # 4. Assertions
    assert len(deduplicated) == 1, 'Should have collapsed to 1 item'
    assert deduplicated[0].id == obs_id, 'The remaining item should be the Observation'

    # Check that metadata was updated with citations
    citations = deduplicated[0].unit_metadata.get('citations')
    assert citations is not None, 'Citations should be added to metadata'
    assert len(citations) == 1

    citation = citations[0]
    assert citation['id'] == str(fact_id)
    assert citation['text'] == fact_text
    # We check the start of the date string to avoid microseconds matching issues if any
    assert citation['date'].startswith('2024-01-20')


def test_deduplicate_no_match(mock_retrieval_engine):
    """
    Test that unrelated units are NOT collapsed.
    """
    u1 = MemoryUnit(
        id=uuid4(),
        text='Fact A',
        fact_type=FactTypes.WORLD,
        event_date=datetime.now(timezone.utc),
        note_id=uuid4(),
        embedding=[],
    )
    u2 = MemoryUnit(
        id=uuid4(),
        text='Fact B',
        fact_type=FactTypes.WORLD,
        event_date=datetime.now(timezone.utc),
        note_id=uuid4(),
        embedding=[],
    )

    results = [u1, u2]
    deduplicated = mock_retrieval_engine._deduplicate_and_cite(results)

    assert len(deduplicated) == 2


def test_deduplicate_missing_evidence(mock_retrieval_engine):
    """
    Test that an Observation citing a missing fact (not in results)
    does not cause errors and does not collapse anything.
    """
    missing_id = uuid4()

    obs_unit = MemoryUnit(
        id=uuid4(),
        text='Observation with missing evidence',
        fact_type='observation',
        event_date=datetime.now(timezone.utc),
        note_id=uuid4(),
        embedding=[],
        unit_metadata={'observation': True, 'evidence_ids': [str(missing_id)]},
    )

    results = [obs_unit]
    deduplicated = mock_retrieval_engine._deduplicate_and_cite(results)

    assert len(deduplicated) == 1
    # Should contain no citations since the evidence wasn't found in the list
    assert deduplicated[0].unit_metadata.get('citations') is None


def test_deduplicate_opinion_logic(mock_retrieval_engine):
    """
    Test that _deduplicate_and_cite collapses a Fact into an Opinion
    if the Fact is listed as evidence, even WITHOUT 'observation': True.
    """
    # 1. Setup Data
    fact_id = uuid4()
    fact_text = 'The user loves spicy curry.'
    fact_date = datetime(2024, 1, 20, 10, 0, 0, tzinfo=timezone.utc)

    fact_unit = MemoryUnit(
        id=fact_id,
        text=fact_text,
        fact_type=FactTypes.WORLD,
        event_date=fact_date,
        note_id=uuid4(),
        embedding=[0.1] * 384,
    )

    op_id = uuid4()
    op_unit = MemoryUnit(
        id=op_id,
        text='It seems the user really likes Indian food.',
        fact_type=FactTypes.OPINION,
        event_date=datetime(2024, 1, 25, tzinfo=timezone.utc),
        note_id=uuid4(),
        embedding=[0.2] * 384,
        unit_metadata={
            # NO 'observation': True flag here!
            # The actual key used by ReasoningEngine for opinions is 'supporting_evidence_ids'
            'supporting_evidence_ids': [str(fact_id)]
        },
    )

    # 2. Simulate Retrieval Result containing BOTH
    results = [op_unit, fact_unit]

    # 3. Apply Deduplication
    deduplicated = mock_retrieval_engine._deduplicate_and_cite(results)

    # 4. Assertions
    assert len(deduplicated) == 1, 'Should have collapsed to 1 item (the Opinion)'
    assert deduplicated[0].id == op_id, 'The remaining item should be the Opinion'

    # Check that metadata was updated with citations
    citations = deduplicated[0].unit_metadata.get('citations')
    assert citations is not None, 'Citations should be added to metadata'
    assert len(citations) == 1

    citation = citations[0]
    assert citation['id'] == str(fact_id)
    assert citation['text'] == fact_text
