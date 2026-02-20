import pytest
from datetime import datetime, timezone

from memex_core.memory.extraction import storage
from memex_core.memory.extraction.models import ProcessedFact
from memex_common.types import FactTypes


@pytest.mark.asyncio
async def test_int_check_duplicates_in_window(session):
    # 1. Insert initial facts
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    existing_fact = ProcessedFact(
        fact_text='The quick brown fox jumps over the lazy dog.',
        embedding=[0.1] * 384,
        fact_type=FactTypes.WORLD,
        payload={},
        occurred_start=base_time,
        mentioned_at=base_time,
    )

    # Store it
    await storage.insert_facts_batch(session, [existing_fact])

    # 2. Prepare new facts to check

    # Duplicate (Text Match)
    dup_text = 'The quick brown fox jumps over the lazy dog.'
    dup_emb = [0.9] * 384  # Different embedding but text matches

    # Duplicate (Semantic Match)
    sem_text = 'A fast brown fox leaped over a lazy dog.'
    sem_emb = [0.1] * 384  # Same embedding (sim=1.0)

    # Non-duplicate
    new_text = 'Something completely different.'
    new_emb = [-0.1] * 384  # Orthogonal/Opposite

    new_texts = [dup_text, sem_text, new_text]
    new_embeddings = [dup_emb, sem_emb, new_emb]

    # 3. Check duplicates
    results = await storage.check_duplicates_in_window(
        session, new_texts, new_embeddings, base_time, window_hours=24, similarity_threshold=0.9
    )

    assert len(results) == 3
    assert results[0] is True, 'Should match by exact text'
    assert results[1] is True, 'Should match by cosine similarity'
    assert results[2] is False, 'Should not match'


@pytest.mark.asyncio
async def test_int_check_duplicates_outside_window(session):
    # 1. Insert fact far in the past
    base_time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    existing_fact = ProcessedFact(
        fact_text='Ancient history.',
        embedding=[0.5] * 384,
        fact_type=FactTypes.WORLD,
        payload={},
        occurred_start=base_time,
        mentioned_at=base_time,
    )
    await storage.insert_facts_batch(session, [existing_fact])

    # 2. Check same fact but currently (2024)
    current_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    results = await storage.check_duplicates_in_window(
        session, ['Ancient history.'], [[0.5] * 384], current_time, window_hours=24
    )

    assert results[0] is False, "Should not match because it's outside the time window"
