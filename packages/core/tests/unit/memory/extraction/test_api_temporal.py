import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from memex_core.memory.extraction.models import ProcessedFact
from memex_core.memory.extraction.pipeline.linking import create_cross_doc_links
from memex_common.types import FactTypes


@pytest.mark.asyncio
async def test_create_cross_doc_links():
    session = AsyncMock()

    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Batch of facts (chronologically ordered for simplicity)
    unit_id_1 = str(uuid4())
    unit_id_2 = str(uuid4())

    facts = [
        ProcessedFact(
            fact_text='Earliest Fact',
            embedding=[0.1] * 384,
            fact_type=FactTypes.WORLD,
            payload={},
            occurred_start=base_time,
            mentioned_at=base_time,
        ),
        ProcessedFact(
            fact_text='Latest Fact',
            embedding=[0.1] * 384,
            fact_type=FactTypes.WORLD,
            payload={},
            occurred_start=base_time + timedelta(hours=1),
            mentioned_at=base_time,
        ),
    ]
    unit_ids = [unit_id_1, unit_id_2]

    predecessor_uuid = uuid4()
    successor_uuid = uuid4()

    with (
        patch('memex_core.memory.extraction.pipeline.linking.storage') as mock_storage,
        patch('memex_core.memory.extraction.pipeline.linking.pg_insert') as mock_pg_insert,
    ):
        # Mock storage responses
        # First call: find_temporal_neighbor(direction='before') -> returns predecessor
        # Second call: find_temporal_neighbor(direction='after') -> returns successor
        mock_storage.find_temporal_neighbor = AsyncMock()
        mock_storage.find_temporal_neighbor.side_effect = [predecessor_uuid, successor_uuid]

        mock_insert_stmt = MagicMock()
        mock_pg_insert.return_value.values.return_value.on_conflict_do_nothing.return_value = (
            mock_insert_stmt
        )

        await create_cross_doc_links(session, unit_ids, facts)

        # Check storage calls
        assert mock_storage.find_temporal_neighbor.call_count == 2

        # Check 'before' search
        call_before = mock_storage.find_temporal_neighbor.call_args_list[0]
        assert call_before[0][1] == facts[0].occurred_start  # earliest_ts
        assert call_before[1]['direction'] == 'before'

        # Check 'after' search
        call_after = mock_storage.find_temporal_neighbor.call_args_list[1]
        assert call_after[0][1] == facts[-1].occurred_start  # latest_ts
        assert call_after[1]['direction'] == 'after'

        # Check Insert
        mock_pg_insert.assert_called()
        values = mock_pg_insert.return_value.values.call_args[0][0]
        assert len(values) == 2

        # Predecessor -> Earliest
        assert values[0]['from_unit_id'] == str(predecessor_uuid)
        assert values[0]['to_unit_id'] == unit_id_1

        # Latest -> Successor
        assert values[1]['from_unit_id'] == unit_id_2
        assert values[1]['to_unit_id'] == str(successor_uuid)
