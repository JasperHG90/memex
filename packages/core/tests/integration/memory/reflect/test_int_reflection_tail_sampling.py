import pytest
import asyncio
from uuid import uuid4
from datetime import datetime, timezone
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.reflect.prompts import CandidateObservation
from memex_core.memory.sql_models import MemoryUnit
from memex_common.config import MemexConfig, GLOBAL_VAULT_ID
from memex_common.types import FactTypes


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase_2_hunt_tail_sampling(session: AsyncSession, memex_config: MemexConfig):
    """
    Verify that Phase 2 Hunt includes tail sampled memories even if not similar.
    """
    # 1. Setup Engine with high tail sampling rate
    memex_config.server.memory.reflection.tail_sampling_rate = 0.5  # 50%
    memex_config.server.memory.reflection.search_limit = 10
    from unittest.mock import MagicMock

    mock_embedder = MagicMock()
    # Mock encode to return a list of embeddings equal to the number of candidates (1)
    import numpy as np

    mock_embedder.encode.return_value = np.array([[0.1] * 384])
    engine = ReflectionEngine(session, config=memex_config, embedder=mock_embedder)

    # 2. Add "Tail" Memories (Random content)
    tail_texts = [
        'The quick brown fox jumps over the lazy dog.',
        'To be or not to be, that is the question.',
        'In a hole in the ground there lived a hobbit.',
        'Call me Ishmael.',
        'It was the best of times, it was the worst of times.',
    ]

    tail_units = []
    for text in tail_texts:
        unit = MemoryUnit(
            id=uuid4(),
            text=text,
            embedding=[0.0] * 384,  # Zero embedding = no similarity to anything
            event_date=datetime.now(timezone.utc),
            fact_type=FactTypes.WORLD,
            vault_id=GLOBAL_VAULT_ID,
        )
        session.add(unit)
        tail_units.append(unit)

    await session.commit()

    # 3. Create a Candidate Observation
    candidate = CandidateObservation(
        content='This is about something completely different like Pineapples.'
    )

    # 4. Run Hunt
    db_lock = asyncio.Lock()
    results = await engine._phase_2_hunt([candidate], db_lock, vault_id=GLOBAL_VAULT_ID)

    # 5. Verify
    assert len(results) == 1
    cand, evidence = results[0]
    assert cand.content == candidate.content

    # We expected some tail memories to be included
    # With rate=0.5 and limit=10, sample_size = max(1, 10 * 0.5 * 10) = 50... wait.
    # My formula was: sample_size = max(1, int(self.config.reflection.search_limit * rate * 10))
    # 10 * 0.5 * 10 = 50. Since we only have 5, we should get all 5.

    evidence_ids = [m.id for m in evidence]
    tail_ids = [m.id for m in tail_units]

    found_tail = [tid for tid in tail_ids if tid in evidence_ids]
    assert len(found_tail) > 0, 'Tail memories should be present in evidence even if not similar'
    # Given my zero embeddings and default threshold, find_similar_facts should return nothing.
    # So all evidence must come from tail sampling.
    assert len(evidence) <= 5
