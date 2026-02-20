import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.entity_resolver import (
    EntityResolver,
    EntityInput,
    EntityCandidate,
    ResolutionResult,
    calculate_match_score,
)

# -- Fixtures --


@pytest.fixture
def resolver():
    return EntityResolver(resolution_threshold=0.65)


@pytest.fixture
def mock_session():
    session = AsyncMock(spec=AsyncSession)
    return session


# -- Pure Logic Tests --


@pytest.mark.parametrize(
    ('name_score', 'co_overlap', 'days_diff', 'expected_range'),
    [
        (1.0, 1.0, 0, (0.99, 1.01)),  # Perfect: 0.5 + 0.3 + 0.2 = 1.0
        (1.0, 0.0, 0, (0.69, 0.71)),  # Name + Time: 0.5 + 0.0 + 0.2 = 0.7
        (1.0, 0.0, 30, (0.59, 0.61)),  # Name + Time (30 days = 1 half-life): 0.5 + 0.0 + 0.1 = 0.6
        (
            0.8,
            0.5,
            30,
            (0.64, 0.66),
        ),  # Mixed: 0.4 + 0.15 + 0.1 = 0.65
    ],
)
def test_calculate_match_score_v2(name_score, co_overlap, days_diff, expected_range):
    candidate = EntityCandidate(
        id=str(uuid4()),
        canonical_name='Test Entity',
        last_seen=datetime.now(timezone.utc) - timedelta(days=days_diff),
        name_similarity_score=name_score,
    )
    input_date = datetime.now(timezone.utc)

    # Simulate overlap
    input_nearby = {'a'}
    known_neighbors = {}
    if co_overlap == 1.0:
        # To get 1.0 matched weight ratio: freq=0 -> 1/log2(2)=1.0
        known_neighbors = {'a': 0}
    elif co_overlap == 0.5:
        # To get 0.5 matched weight ratio: freq=2 -> 1/log2(4)=0.5
        known_neighbors = {'a': 2}

    score = calculate_match_score(candidate, input_date, input_nearby, known_neighbors)
    assert expected_range[0] <= score <= expected_range[1]


def test_prepare_inputs_v2(resolver):
    raw_data = [
        {
            'text': 'Entity A',
            'event_date': datetime(2023, 1, 1),
            'nearby_entities': [
                {'text': 'Entity B'},
                {'text': 'Entity A'},
            ],
        },
        {
            'text': 'entity a',  # Duplicate
            'nearby_entities': [{'text': 'Entity C'}],
        },
    ]
    default_date = datetime(2023, 1, 1, tzinfo=timezone.utc)

    inputs = resolver._prepare_inputs(raw_data, default_date)

    assert len(inputs) == 1  # Deduplicated
    inp = inputs[0]
    assert inp.text == 'Entity A'
    assert inp.indices == [0, 1]
    assert 'entity b' in inp.nearby_entity_names
    assert 'entity c' in inp.nearby_entity_names
    assert 'entity a' not in inp.nearby_entity_names


# -- Async/DB Tests --


@pytest.mark.asyncio
async def test_fetch_candidates_v2(resolver, mock_session):
    # Mock result
    # Columns: idx, id, canonical_name, last_seen, name_score, is_phonetic
    mock_rows = [
        (0, uuid4(), 'Entity One', datetime.now(timezone.utc), 0.8, False),
    ]
    mock_result = MagicMock()
    mock_result.__iter__.return_value = mock_rows
    mock_session.exec.return_value = mock_result

    inp = EntityInput(
        index=0,
        indices=[0],
        text='test',
        event_date=datetime.now(timezone.utc),
        nearby_entity_names=set(),
    )
    candidates = await resolver._fetch_candidates(mock_session, [inp])

    assert len(candidates) == 1
    assert len(candidates[0]) == 1
    assert candidates[0][0].canonical_name == 'Entity One'
    assert candidates[0][0].name_similarity_score == 0.8


@pytest.mark.asyncio
async def test_fetch_neighbor_map_v2(resolver, mock_session):
    # Columns: source_id, canonical_name, mention_count
    id1 = uuid4()
    mock_rows = [
        (id1, 'Entity Two', 10),
    ]
    mock_result = MagicMock()
    mock_result.__iter__.return_value = mock_rows
    mock_session.exec.return_value = mock_result

    m = await resolver._fetch_neighbor_map(mock_session, [str(id1)])

    assert str(id1) in m
    assert 'entity two' in m[str(id1)]
    assert m[str(id1)]['entity two'] == 10


@pytest.mark.asyncio
async def test_persist_resolutions_v2(resolver, mock_session):
    # Case 1: Existing Entity (Update)
    inp_update = EntityInput(
        index=0,
        indices=[0],
        text='Existing',
        event_date=datetime.now(timezone.utc),
        nearby_entity_names=set(),
    )
    res_update = ResolutionResult(
        input_indices=[0], entity_id='existing-uuid', is_new=False, input_data=inp_update
    )

    # Case 2: New Entity (Insert)
    inp_new = EntityInput(
        index=1,
        indices=[1],
        text='New Entity',
        event_date=datetime.now(timezone.utc),
        nearby_entity_names=set(),
    )
    res_new = ResolutionResult(input_indices=[1], input_data=inp_new, is_new=True)

    resolutions = [res_update, res_new]

    # Mock return for Insert (upsert)
    new_id = uuid4()
    mock_insert_result = MagicMock()
    mock_insert_result.all.return_value = [(new_id, 'New Entity')]

    # Calls: 1. Update, 2. Alias Insert, 3. Entity Insert
    mock_session.exec.side_effect = [
        MagicMock(),  # Update result
        MagicMock(),  # Alias result
        mock_insert_result,  # Insert result
    ]

    final_ids = await resolver._persist_resolutions(mock_session, resolutions)

    assert len(final_ids) == 2
    assert final_ids[0] == 'existing-uuid'
    assert final_ids[1] == str(new_id)
    assert mock_session.exec.call_count == 3
