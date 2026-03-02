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


def test_deduplicate_and_cite_keeps_both(mock_retrieval_engine):
    """
    Test that _deduplicate_and_cite keeps BOTH the Observation and its cited Fact
    in the results, and attaches citation metadata to the Observation.
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

    # 4. Assertions — both items should remain
    assert len(deduplicated) == 2, 'Both observation and fact should remain in results'
    result_ids = {u.id for u in deduplicated}
    assert obs_id in result_ids, 'Observation should be in results'
    assert fact_id in result_ids, 'Cited fact should also remain in results'

    # Check that metadata was updated with citations on the observation
    obs_result = next(u for u in deduplicated if u.id == obs_id)
    citations = obs_result.unit_metadata.get('citations')
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


def test_deduplicate_opinion_keeps_both(mock_retrieval_engine):
    """
    Test that _deduplicate_and_cite keeps BOTH a Fact and an Opinion that cites it,
    and attaches citation metadata to the Opinion.
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

    # 4. Assertions — both items should remain
    assert len(deduplicated) == 2, 'Both opinion and fact should remain in results'
    result_ids = {u.id for u in deduplicated}
    assert op_id in result_ids, 'Opinion should be in results'
    assert fact_id in result_ids, 'Cited fact should also remain in results'

    # Check that metadata was updated with citations on the opinion
    op_result = next(u for u in deduplicated if u.id == op_id)
    citations = op_result.unit_metadata.get('citations')
    assert citations is not None, 'Citations should be added to metadata'
    assert len(citations) == 1

    citation = citations[0]
    assert citation['id'] == str(fact_id)
    assert citation['text'] == fact_text


def test_deduplicate_self_reference_ignored(mock_retrieval_engine):
    """
    Test that a unit referencing itself in evidence_ids does not cause issues.
    """
    self_id = uuid4()
    unit = MemoryUnit(
        id=self_id,
        text='Self-referencing unit',
        fact_type='observation',
        event_date=datetime.now(timezone.utc),
        note_id=uuid4(),
        embedding=[],
        unit_metadata={'evidence_ids': [str(self_id)]},
    )

    results = [unit]
    deduplicated = mock_retrieval_engine._deduplicate_and_cite(results)

    assert len(deduplicated) == 1
    assert deduplicated[0].unit_metadata.get('citations') is None


def test_deduplicate_multiple_observations_same_fact(mock_retrieval_engine):
    """
    Test that multiple observations citing the same fact all get citations,
    and the fact remains in results.
    """
    fact_id = uuid4()
    fact_text = 'User works at Acme Corp.'

    fact_unit = MemoryUnit(
        id=fact_id,
        text=fact_text,
        fact_type=FactTypes.WORLD,
        event_date=datetime(2024, 1, 10, tzinfo=timezone.utc),
        note_id=uuid4(),
        embedding=[0.1] * 384,
    )

    obs1 = MemoryUnit(
        id=uuid4(),
        text='[Career] The user is employed at Acme Corp.',
        fact_type='observation',
        event_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
        note_id=uuid4(),
        embedding=[0.2] * 384,
        unit_metadata={'observation': True, 'evidence_ids': [str(fact_id)]},
    )

    obs2 = MemoryUnit(
        id=uuid4(),
        text='[Professional] User has corporate experience at Acme.',
        fact_type='observation',
        event_date=datetime(2024, 2, 5, tzinfo=timezone.utc),
        note_id=uuid4(),
        embedding=[0.3] * 384,
        unit_metadata={'observation': True, 'evidence_ids': [str(fact_id)]},
    )

    results = [obs1, fact_unit, obs2]
    deduplicated = mock_retrieval_engine._deduplicate_and_cite(results)

    # All three should remain
    assert len(deduplicated) == 3

    # Both observations should have citations
    for obs in [obs1, obs2]:
        obs_result = next(u for u in deduplicated if u.id == obs.id)
        citations = obs_result.unit_metadata.get('citations', [])
        assert len(citations) == 1
        assert citations[0]['id'] == str(fact_id)
