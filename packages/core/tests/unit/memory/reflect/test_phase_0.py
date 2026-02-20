import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import MentalModel, Observation, MemoryUnit
from memex_core.memory.reflect.prompts import (
    UpdatedObservationResult,
    NewEvidenceItem,
)


@pytest.mark.asyncio
async def test_phase_0_update_logic_happy_path():
    """
    Test that Phase 0 correctly maps integer indices back to memory UUIDs
    and updates the observation.
    """
    # 1. Setup Data
    entity_id = uuid4()
    mem_1_id = uuid4()
    mem_2_id = uuid4()

    # Existing Observation
    obs = Observation(
        title='User likes Python', content='The user prefers Python over JS.', evidence=[]
    )

    model = MentalModel(
        entity_id=entity_id, name='Test Entity', observations=[obs.model_dump(mode='json')]
    )

    # Recent Memories (The Context)
    memories = [
        MemoryUnit(
            id=mem_1_id,
            content='I wrote a script in Python today.',
            event_date=datetime.now(timezone.utc),
        ),
        MemoryUnit(
            id=mem_2_id, content='I also installed uv.', event_date=datetime.now(timezone.utc)
        ),
    ]

    # 2. Mock Engine, DSPy & Config
    mock_config = MagicMock()
    # Ensure config.server.memory.extraction.max_concurrency and model_name are safe if accessed
    mock_config.server.memory.extraction.max_concurrency = 5
    mock_config.server.memory.extraction.model.model = 'test-model'

    engine = ReflectionEngine(session=AsyncMock(), config=mock_config, embedder=MagicMock())
    engine.lm = MagicMock()  # Mock LM presence

    # Mock the run_dspy_operation result
    # The LLM says: Memory index 0 supports Observation index 0
    mock_updates = [
        UpdatedObservationResult(
            observation_index=0,
            new_evidence=[
                NewEvidenceItem(
                    memory_id=0,  # Should map to mem_1_id
                    quote='I wrote a script in Python',
                    relevance_explanation='Direct confirmation',
                    timestamp='2025-01-01',
                )
            ],
            has_contradiction=False,
        )
    ]

    mock_result = MagicMock()
    mock_result.updates = mock_updates

    # Patch run_dspy_operation to return our mock
    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (mock_result, None)

        # 3. Execute
        updated_obs_list = await engine._phase_0_update(model, 'Test Entity', memories)

    # 4. Verify
    assert len(updated_obs_list) == 1
    updated_obs = updated_obs_list[0]

    # Check Evidence was added
    assert len(updated_obs.evidence) == 1
    evidence = updated_obs.evidence[0]

    assert evidence.memory_id == mem_1_id
    assert evidence.quote == 'I wrote a script in Python'
    assert evidence.relevance == 1.0


@pytest.mark.asyncio
async def test_phase_0_contradiction_handling():
    """Test that contradictions act as expected."""
    model = MentalModel(
        entity_id=uuid4(),
        name='Test',
        observations=[{'title': 'A', 'content': 'Sky is blue', 'evidence': []}],
    )
    memories = [MemoryUnit(id=uuid4(), content='Sky is green')]

    mock_config = MagicMock()
    engine = ReflectionEngine(session=AsyncMock(), config=mock_config, embedder=MagicMock())
    engine.lm = MagicMock()

    mock_updates = [
        UpdatedObservationResult(
            observation_index=0,
            new_evidence=[],
            has_contradiction=True,
            contradiction_note='User claims sky is green',
        )
    ]
    mock_result = MagicMock()
    mock_result.updates = mock_updates

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (mock_result, None)
        updated = await engine._phase_0_update(model, 'Test', memories)

    assert '[CONTRADICTION: User claims sky is green]' in updated[0].content


@pytest.mark.asyncio
async def test_phase_0_none_id_handling():
    """Test that None memory_id is safely ignored."""
    model = MentalModel(
        entity_id=uuid4(),
        name='Test',
        observations=[{'title': 'A', 'content': 'B', 'evidence': []}],
    )
    memories = [MemoryUnit(id=uuid4(), content='Something')]

    mock_config = MagicMock()
    engine = ReflectionEngine(session=AsyncMock(), config=mock_config, embedder=MagicMock())
    engine.lm = MagicMock()

    mock_updates = [
        UpdatedObservationResult(
            observation_index=0,
            new_evidence=[
                NewEvidenceItem(
                    memory_id=None,  # The specific test case
                    quote='General knowledge',
                    relevance_explanation='N/A',
                    timestamp='2025',
                )
            ],
            has_contradiction=False,
        )
    ]

    mock_result = MagicMock()
    mock_result.updates = mock_updates

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (mock_result, None)
        updated = await engine._phase_0_update(model, 'Test', memories)

    # Should have 0 evidence added because ID was None
    assert len(updated[0].evidence) == 0


@pytest.mark.asyncio
async def test_phase_0_out_of_bounds_handling():
    """Test that invalid indices don't crash the loop."""
    model = MentalModel(
        entity_id=uuid4(),
        name='Test',
        observations=[{'title': 'A', 'content': 'B', 'evidence': []}],
    )
    memories = [MemoryUnit(id=uuid4(), content='X')]  # Length 1

    mock_config = MagicMock()
    engine = ReflectionEngine(session=AsyncMock(), config=mock_config, embedder=MagicMock())
    engine.lm = MagicMock()

    mock_updates = [
        UpdatedObservationResult(
            observation_index=0,
            new_evidence=[
                NewEvidenceItem(
                    memory_id=99,  # Does not exist
                    quote='Hallucinated',
                    relevance_explanation='...',
                    timestamp='2025',
                )
            ],
            has_contradiction=False,
        )
    ]

    mock_result = MagicMock()
    mock_result.updates = mock_updates

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (mock_result, None)
        updated = await engine._phase_0_update(model, 'Test', memories)

    # Should process safely and add no evidence
    assert len(updated[0].evidence) == 0
