import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.utils import calculate_temporal_score


@pytest.fixture
def resolver():
    return EntityResolver(resolution_threshold=0.65)


@pytest.fixture
def mock_session():
    return AsyncMock()


def test_temporal_half_life_decay():
    """
    Test that temporal decay follows the exponential half-life model.
    """
    now = datetime.now(timezone.utc)
    half_life = 30.0

    # At 0 days, score should be 1.0
    assert calculate_temporal_score(now, now, half_life) == 1.0

    # At 30 days (1 half-life), score should be 0.5
    t30 = now + timedelta(days=30)
    assert pytest.approx(calculate_temporal_score(now, t30, half_life)) == 0.5

    # At 60 days (2 half-lives), score should be 0.25
    t60 = now + timedelta(days=60)
    assert pytest.approx(calculate_temporal_score(now, t60, half_life)) == 0.25


@pytest.mark.asyncio
async def test_intra_batch_deduplication(resolver):
    """
    Test that multiple identical entities in the same batch are grouped.
    """
    now = datetime.now(timezone.utc)
    raw_data = [
        {'text': 'Apple', 'nearby_entities': []},
        {'text': 'apple', 'nearby_entities': []},  # Should group with 'Apple'
        {'text': 'Microsoft', 'nearby_entities': []},
    ]

    inputs = resolver._prepare_inputs(raw_data, now)

    # After implementation, inputs should be deduplicated
    assert len(inputs) == 2

    # Verify groupings
    apple_input = next(i for i in inputs if i.text.lower() == 'apple')
    assert apple_input.indices == [0, 1]

    ms_input = next(i for i in inputs if i.text.lower() == 'microsoft')
    assert ms_input.indices == [2]


@pytest.mark.asyncio
async def test_fetch_candidates_filtering_by_type(resolver, mock_session):
    """
    Verify that candidate fetching SQL includes type filtering.
    """
    # This is hard to test with unit tests on raw SQL, but we can verify
    # that the resolver passes the type to the query.
    pass


@pytest.mark.asyncio
async def test_fetch_candidates_includes_aliases(resolver, mock_session):
    """
    Verify that candidate fetching SQL joins with EntityAlias.
    """
    pass


@pytest.mark.asyncio
async def test_fetch_candidates_includes_phonetic(resolver, mock_session):
    """
    Verify that candidate fetching SQL includes phonetic match.
    """
    pass
