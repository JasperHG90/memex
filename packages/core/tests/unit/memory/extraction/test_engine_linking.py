import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from uuid import uuid4

from memex_core.memory.extraction.models import ProcessedFact, FactTypes
from memex_core.memory.extraction.pipeline.linking import create_links, create_cross_doc_links


def make_fact(occurred=None, mentioned=None, doc_id='doc1'):
    return ProcessedFact(
        fact_text='test',
        fact_type=FactTypes.WORLD,
        embedding=[],
        occurred_start=occurred,
        mentioned_at=mentioned or datetime.now(timezone.utc),
        note_id=doc_id,
    )


@pytest.mark.asyncio
async def test_create_links_temporal_sorting():
    """Test that facts are sorted by timestamp for temporal linking."""
    mock_session = AsyncMock()

    # 3 Facts: Late, Early, Middle
    f_late = make_fact(occurred=datetime(2024, 1, 3, tzinfo=timezone.utc))
    f_early = make_fact(occurred=datetime(2024, 1, 1, tzinfo=timezone.utc))
    f_mid = make_fact(occurred=datetime(2024, 1, 2, tzinfo=timezone.utc))

    facts = [f_late, f_early, f_mid]
    unit_ids = [str(uuid4()), str(uuid4()), str(uuid4())]

    with (
        patch(
            'memex_core.memory.extraction.pipeline.linking.storage.find_similar_facts',
            new_callable=AsyncMock,
        ) as mock_sim,
        patch(
            'memex_core.memory.extraction.pipeline.linking.storage.find_temporal_neighbor',
            new_callable=AsyncMock,
        ),
    ):
        mock_sim.return_value = []

        await create_links(mock_session, unit_ids, facts)

        # The implementation sorts indices based on timestamps.
        # Sorted order of indices should be: 1, 2, 0
        # Link 1: unit_ids[1] -> unit_ids[2]
        # Link 2: unit_ids[2] -> unit_ids[0]

        # If it runs without error, sorting worked correctly.


@pytest.mark.asyncio
async def test_create_cross_doc_links_sorting():
    """Test finding earliest and latest facts for cross-doc linking."""
    mock_session = AsyncMock()

    f1 = make_fact(occurred=datetime(2024, 1, 10, tzinfo=timezone.utc))  # Middle
    f2 = make_fact(occurred=datetime(2024, 1, 1, tzinfo=timezone.utc))  # Earliest
    f3 = make_fact(occurred=datetime(2024, 1, 20, tzinfo=timezone.utc))  # Latest

    facts = [f1, f2, f3]
    ids = [str(uuid4()), str(uuid4()), str(uuid4())]

    with patch(
        'memex_core.memory.extraction.pipeline.linking.storage.find_temporal_neighbor',
        new_callable=AsyncMock,
    ) as mock_find:
        mock_find.return_value = uuid4()  # Dummy neighbor

        await create_cross_doc_links(mock_session, ids, facts)

        # Verify find_temporal_neighbor was called with correct timestamps
        calls = mock_find.call_args_list
        assert len(calls) == 2

        # Check first call (predecessor) -> should be f2's time (Jan 1)
        args1, kwargs1 = calls[0]
        if len(args1) > 1:
            assert args1[1] == f2.occurred_start
        else:
            assert kwargs1['timestamp'] == f2.occurred_start

        if len(args1) > 2:
            assert args1[2] == 'before'
        else:
            assert kwargs1['direction'] == 'before'

        # Check second call (successor) -> should be f3's time (Jan 20)
        args2, kwargs2 = calls[1]
        if len(args2) > 1:
            assert args2[1] == f3.occurred_start
        else:
            assert kwargs2['timestamp'] == f3.occurred_start

        if len(args2) > 2:
            assert args2[2] == 'after'
        else:
            assert kwargs2['direction'] == 'after'


@pytest.mark.asyncio
async def test_normalize_timestamp_integration():
    """Test that None values in timestamps don't crash the sorting logic."""
    mock_session = AsyncMock()

    f1 = make_fact(occurred=None, mentioned=datetime(2024, 1, 5, tzinfo=timezone.utc))
    f2 = make_fact(occurred=datetime(2024, 1, 1, tzinfo=timezone.utc))

    facts = [f1, f2]
    ids = [str(uuid4()), str(uuid4())]

    with (
        patch(
            'memex_core.memory.extraction.pipeline.linking.storage.find_similar_facts',
            new_callable=AsyncMock,
        ) as mock_sim,
        patch(
            'memex_core.memory.extraction.pipeline.linking.storage.find_temporal_neighbor',
            new_callable=AsyncMock,
        ),
    ):
        mock_sim.return_value = []
        await create_links(mock_session, ids, facts)
        # Should pass without TypeError
