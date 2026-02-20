import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta

from memex_core.memory.extraction import deduplication
from memex_core.memory.extraction.models import ProcessedFact
from memex_common.types import FactTypes


@pytest.mark.asyncio
async def test_check_duplicates_batch():
    session = AsyncMock()

    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    facts = [
        ProcessedFact(
            fact_text='Fact 1',
            embedding=[0.1] * 384,
            fact_type=FactTypes.WORLD,
            payload={},
            occurred_start=base_time,
            mentioned_at=base_time,
        ),
        ProcessedFact(
            fact_text='Fact 2',
            embedding=[0.2] * 384,
            fact_type=FactTypes.WORLD,
            payload={},
            occurred_start=base_time + timedelta(hours=2),
            mentioned_at=base_time,
        ),
        ProcessedFact(
            fact_text='Fact 3',
            embedding=[0.3] * 384,
            fact_type=FactTypes.WORLD,
            payload={},
            occurred_start=base_time + timedelta(hours=20),  # Different bucket likely
            mentioned_at=base_time,
        ),
    ]

    async def mock_checker(conn, texts, embeddings, date, window_hours, vault_ids=None):
        # Determine which facts are in this batch based on texts
        results = []
        for text in texts:
            if text == 'Fact 1':
                results.append(True)  # Duplicate
            else:
                results.append(False)
        return results

    results = await deduplication.check_duplicates_batch(session, facts, mock_checker)

    assert len(results) == 3
    assert results[0] is True  # Fact 1
    assert results[1] is False  # Fact 2
    assert results[2] is False  # Fact 3


def test_filter_duplicates():
    facts = [MagicMock(spec=ProcessedFact) for _ in range(3)]
    flags = [True, False, True]

    filtered = deduplication.filter_duplicates(facts, flags)

    assert len(filtered) == 1
    assert filtered[0] == facts[1]
