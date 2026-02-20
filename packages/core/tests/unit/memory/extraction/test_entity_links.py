from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.extraction import entity_links
from memex_core.memory.extraction.models import EntityLink

# --- Fixtures ---


@pytest.fixture
def mock_session():
    session = AsyncMock(spec=AsyncSession)
    # Default behavior for exec: return a mock that has .all() returning empty list
    mock_result = MagicMock()
    mock_result.all.return_value = []
    session.exec.return_value = mock_result
    return session


@pytest.fixture
def sample_uuids():
    return [uuid4() for _ in range(5)]


@pytest.fixture
def sample_dates():
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return [base + timedelta(hours=i) for i in range(5)]


# --- Pure Logic Tests ---


def test_normalize_datetime_none():
    assert entity_links._normalize_datetime(None) is None


def test_normalize_datetime_naive():
    dt = datetime(2024, 1, 1, 12, 0, 0)
    norm = entity_links._normalize_datetime(dt)
    assert norm.tzinfo == timezone.utc
    assert norm.year == 2024


def test_normalize_datetime_aware():
    dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    norm = entity_links._normalize_datetime(dt)
    assert norm == dt


def test_compute_temporal_links_empty():
    assert entity_links.compute_temporal_links({}, []) == []


def test_compute_temporal_links_basic(sample_uuids, sample_dates):
    u1, u2 = sample_uuids[0], sample_uuids[1]
    d1, d2 = sample_dates[0], sample_dates[1]  # 1 hour diff

    new_units = {str(u1): d1}
    candidates = [(u2, d2)]

    links = entity_links.compute_temporal_links(new_units, candidates, time_window_hours=24)

    assert len(links) == 1
    link = links[0]
    assert link['from_unit_id'] == u1
    assert link['to_unit_id'] == u2
    assert link['link_type'] == 'temporal'
    # Weight calc: 1.0 - (1 / 24) = ~0.958
    assert 0.9 < link['weight'] < 1.0


def test_compute_temporal_links_outside_window(sample_uuids, sample_dates):
    u1, u2 = sample_uuids[0], sample_uuids[1]
    d1 = sample_dates[0]
    d2 = d1 + timedelta(hours=25)  # Outside 24h window

    new_units = {str(u1): d1}
    candidates = [(u2, d2)]

    links = entity_links.compute_temporal_links(new_units, candidates, time_window_hours=24)
    assert len(links) == 0


def test_compute_temporal_links_self_exclusion(sample_uuids, sample_dates):
    u1 = sample_uuids[0]
    d1 = sample_dates[0]

    new_units = {str(u1): d1}
    candidates = [(u1, d1)]  # Same unit

    links = entity_links.compute_temporal_links(new_units, candidates)
    assert len(links) == 0


def test_flatten_llm_entities(sample_dates):
    unit_ids = ['u1', 'u2']
    dates = sample_dates[:2]
    llm_entities = [
        [{'text': 'Apple', 'type': 'ORG'}],  # u1
        [{'text': 'Banana'}],  # u2 (missing type)
    ]

    flat, idx_map = entity_links._flatten_llm_entities(unit_ids, llm_entities, dates)

    assert len(flat) == 2
    assert flat[0]['text'] == 'Apple'
    assert flat[0]['event_date'] == dates[0]
    assert idx_map[0] == 0  # u1

    assert flat[1]['text'] == 'Banana'
    assert flat[1]['event_date'] == dates[1]
    assert idx_map[1] == 1  # u2


def test_generate_entity_graph_links(sample_uuids):
    u_new1, u_new2 = sample_uuids[0], sample_uuids[1]
    u_exist1 = sample_uuids[2]
    e_uuid = uuid4()

    entity_to_units = {str(e_uuid): [u_new1, u_new2, u_exist1]}
    new_unit_ids = [str(u_new1), str(u_new2)]

    links = entity_links._generate_entity_graph_links(entity_to_units, new_unit_ids)

    # Expect:
    # New <-> New: u_new1 <-> u_new2 (2 links)
    # New <-> Exist: u_new1 <-> u_exist1 (2 links)
    # New <-> Exist: u_new2 <-> u_exist1 (2 links)
    # Total = 6
    assert len(links) == 6

    # Verify types
    assert all(isinstance(link, EntityLink) for link in links)
    assert all(link.entity_id == e_uuid for link in links)


def test_build_causal_link_data(sample_uuids):
    u1, u2 = sample_uuids[0], sample_uuids[1]
    unit_ids = [str(u1), str(u2)]

    # u1 causes u2
    relations = [
        [{'target_fact_index': 1, 'relation_type': 'causes', 'strength': 0.8}],  # u1's relations
        [],  # u2's relations
    ]

    links = entity_links._build_causal_link_data(unit_ids, relations)

    assert len(links) == 1
    assert links[0]['from_unit_id'] == u1
    assert links[0]['to_unit_id'] == u2
    assert links[0]['link_type'] == 'causes'
    assert links[0]['weight'] == 0.8


def test_build_causal_link_data_invalid(sample_uuids):
    u1 = sample_uuids[0]
    unit_ids = [str(u1)]

    relations = [
        [
            {'target_fact_index': 99, 'relation_type': 'causes'},  # Index out of bounds
            {'target_fact_index': 0, 'relation_type': 'causes'},  # Self link
            {'target_fact_index': 0, 'relation_type': 'invalid'},  # Invalid type
        ]
    ]

    links = entity_links._build_causal_link_data(unit_ids, relations)
    assert len(links) == 0


# --- Async/DB Tests ---


@pytest.mark.asyncio
async def test_bulk_insert_memory_links(mock_session):
    links_data = [
        {'from_unit_id': uuid4(), 'to_unit_id': uuid4(), 'link_type': 't', 'entity_id': None}
    ]

    # Patch insert to avoid DB errors and verify it's called
    with patch('memex_core.memory.extraction.entity_links.insert') as mock_insert:
        mock_stmt = MagicMock()
        mock_insert.return_value.values.return_value.on_conflict_do_nothing.return_value = mock_stmt

        await entity_links._bulk_insert_memory_links(mock_session, links_data)

        mock_session.exec.assert_awaited_once_with(mock_stmt)


@pytest.mark.asyncio
async def test_create_temporal_links_batch_per_fact(mock_session, sample_uuids, sample_dates):
    u1 = str(sample_uuids[0])
    unit_ids = [u1]

    # Mock internal helpers
    with (
        patch(
            'memex_core.memory.extraction.entity_links._get_unit_dates', new_callable=AsyncMock
        ) as mock_get_dates,
        patch(
            'memex_core.memory.extraction.entity_links._get_temporal_candidates',
            new_callable=AsyncMock,
        ) as mock_get_cands,
        patch(
            'memex_core.memory.extraction.entity_links._bulk_insert_memory_links',
            new_callable=AsyncMock,
        ) as mock_insert,
    ):
        mock_get_dates.return_value = {u1: sample_dates[0]}
        # Return one candidate
        mock_get_cands.return_value = [(sample_uuids[1], sample_dates[1])]

        count = await entity_links.create_temporal_links_batch_per_fact(mock_session, unit_ids)

        assert count == 1
        mock_insert.assert_awaited_once()
        args, _ = mock_insert.call_args
        assert len(args[1]) == 1  # links_data list


@pytest.mark.asyncio
async def test_extract_entities_batch_optimized(mock_session, sample_uuids):
    unit_ids = [str(sample_uuids[0])]
    sentences = ['text']
    context = 'context'
    fact_dates = [datetime.now()]
    llm_entities = [[{'text': 'E1'}]]

    mock_resolver = AsyncMock()
    # Return one resolved entity ID
    mock_resolver.resolve_entities_batch.return_value = [str(uuid4())]

    with patch(
        'memex_core.memory.extraction.entity_links._fetch_units_by_entity_ids',
        new_callable=AsyncMock,
    ) as mock_fetch_units:
        mock_fetch_units.return_value = {str(uuid4()): [sample_uuids[0], sample_uuids[1]]}

        links = await entity_links.extract_entities_batch_optimized(
            mock_resolver, mock_session, unit_ids, sentences, context, fact_dates, llm_entities
        )

        mock_resolver.resolve_entities_batch.assert_awaited_once()
        mock_resolver.link_units_to_entities_batch.assert_awaited_once()
        assert len(links) > 0  # Should generate graph links


@pytest.mark.asyncio
async def test_create_semantic_links_batch(mock_session, sample_uuids):
    u1 = str(sample_uuids[0])
    embeddings = [[1.0, 0.0]]

    with (
        patch(
            'memex_core.memory.extraction.storage.find_similar_facts',
            new_callable=AsyncMock,
        ) as mock_find,
        patch(
            'memex_core.memory.extraction.entity_links._bulk_insert_memory_links',
            new_callable=AsyncMock,
        ) as mock_insert,
    ):
        # Return an existing unit with similarity score
        mock_find.return_value = []

        # Threshold 0.5 should filter it out (if we returned something below it, but here we return empty)
        count = await entity_links.create_semantic_links_batch(
            mock_session, [u1], embeddings, threshold=0.5
        )

        # Expect 0 because empty result
        assert count == 0

        # Now try with a result
        mock_find.return_value = [(sample_uuids[1], 0.9)]
        count = await entity_links.create_semantic_links_batch(
            mock_session, [u1], embeddings, threshold=0.5
        )

        # Expect 1 link
        assert count == 1
        mock_insert.assert_awaited()


@pytest.mark.asyncio
async def test_create_causal_links_batch(mock_session, sample_uuids):
    u1 = str(sample_uuids[0])
    # Relations: u1 causes u2 (index 1 which is missing in unit_ids list, so ignored? No, wait)
    # The function expects target_fact_index to refer to an index in unit_ids?
    # Checking code: `if target_idx is None or not (0 <= target_idx < len(unit_ids)): continue`
    # So yes, it only links within the batch.

    u2 = str(sample_uuids[1])
    unit_ids = [u1, u2]
    relations = [[{'target_fact_index': 1, 'relation_type': 'causes'}], []]

    with patch(
        'memex_core.memory.extraction.entity_links._bulk_insert_memory_links',
        new_callable=AsyncMock,
    ) as mock_insert:
        count = await entity_links.create_causal_links_batch(mock_session, unit_ids, relations)

        assert count == 1
        mock_insert.assert_awaited_once()


@pytest.mark.asyncio
async def test_insert_entity_links_batch(mock_session):
    link = EntityLink(from_unit_id=uuid4(), to_unit_id=uuid4(), entity_id=uuid4())

    with patch(
        'memex_core.memory.extraction.entity_links._bulk_insert_memory_links',
        new_callable=AsyncMock,
    ) as mock_insert:
        await entity_links.insert_entity_links_batch(mock_session, [link])
        mock_insert.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_causal_links_batch_mismatch(mock_session):
    unit_ids = ['u1']
    relations: list[list[dict]] = [[], []]  # 2 items vs 1 unit_id

    with pytest.raises(ValueError, match='Mismatch between unit_ids'):
        await entity_links.create_causal_links_batch(mock_session, unit_ids, relations)


@pytest.mark.asyncio
async def test_create_temporal_links_batch_wrapper(mock_session):
    unit_ids = ['u1', 'u2']

    with patch(
        'memex_core.memory.extraction.entity_links.create_temporal_links_batch_per_fact',
        new_callable=AsyncMock,
    ) as mock_impl:
        mock_impl.return_value = 5

        result = await entity_links.create_temporal_links_batch(
            mock_session, unit_ids, time_window_hours=48
        )

        assert result == 5
        mock_impl.assert_awaited_once_with(mock_session, unit_ids, 48)
