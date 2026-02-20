from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone

import pytest
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.models import ProcessedFact
from memex_core.config import ExtractionConfig, ConfidenceConfig
from memex_common.types import FactTypes


@pytest.fixture
def mock_storage():
    with patch('memex_core.memory.extraction.engine.storage') as mock:
        yield mock


@pytest.fixture
def extractor(mock_storage):
    config = ExtractionConfig()
    confidence_config = ConfidenceConfig()
    lm = MagicMock()
    predictor = MagicMock()
    embedding_model = MagicMock()
    entity_resolver = MagicMock()
    return ExtractionEngine(
        config, confidence_config, lm, predictor, embedding_model, entity_resolver
    )


@pytest.mark.asyncio
async def test_create_links_semantic(extractor, mock_storage):
    """Test that semantic links are created based on similarity search."""
    session = AsyncMock()

    # Setup data
    unit_id_1 = str(uuid4())
    unit_id_2 = str(uuid4())
    unit_ids = [unit_id_1, unit_id_2]

    fact_1 = ProcessedFact(
        fact_text='Fact 1',
        embedding=[0.1] * 384,
        fact_type=FactTypes.WORLD,
        payload={},
        occurred_start=None,
        mentioned_at=datetime.now(timezone.utc),
    )
    fact_2 = ProcessedFact(
        fact_text='Fact 2',
        embedding=[0.2] * 384,
        fact_type=FactTypes.WORLD,
        payload={},
        occurred_start=None,
        mentioned_at=datetime.now(timezone.utc),
    )
    facts = [fact_1, fact_2]

    # Mock storage.find_similar_facts
    # Return a match for fact_1 (simulating it matched an existing fact_3)
    target_uuid = uuid4()
    mock_storage.find_similar_facts = AsyncMock()
    mock_storage.find_similar_facts.side_effect = [
        [(target_uuid, 0.85)],  # Result for fact_1
        [],  # Result for fact_2
    ]
    mock_storage.find_temporal_neighbor = AsyncMock()

    # Call the method
    await extractor._create_links(session, unit_ids, facts)

    # Verify find_similar_facts calls
    assert mock_storage.find_similar_facts.call_count == 2

    # Check arguments for first call
    call_args = mock_storage.find_similar_facts.call_args_list[0]
    assert call_args[0][1] == fact_1.embedding
    assert call_args[1]['limit'] == 5
    assert call_args[1]['threshold'] == 0.75

    # Verify DB insert for links
    # We expect 1 semantic link: unit_id_1 -> target_uuid
    assert session.exec.called


@pytest.mark.asyncio
async def test_create_links_semantic_verify_values(extractor):
    """Verify the exact values passed to DB insert."""
    session = AsyncMock()

    unit_id_1 = str(uuid4())
    facts = [
        ProcessedFact(
            fact_text='Fact 1',
            embedding=[0.1] * 384,
            fact_type=FactTypes.WORLD,
            payload={},
            occurred_start=None,
            mentioned_at=datetime.now(timezone.utc),
        )
    ]
    unit_ids = [unit_id_1]

    target_uuid = uuid4()

    with (
        patch('memex_core.memory.extraction.engine.storage') as mock_storage,
        patch('memex_core.memory.extraction.engine.pg_insert') as mock_pg_insert,
    ):
        mock_storage.find_similar_facts = AsyncMock()
        mock_storage.find_similar_facts.return_value = [(target_uuid, 0.9)]
        mock_insert_stmt = MagicMock()
        mock_pg_insert.return_value.values.return_value.on_conflict_do_nothing.return_value = (
            mock_insert_stmt
        )
        mock_storage.find_temporal_neighbor = AsyncMock(return_value=None)

        await extractor._create_links(session, unit_ids, facts)

        # Verify pg_insert values
        mock_pg_insert.assert_called()
        values_call = mock_pg_insert.return_value.values.call_args[0][0]

        assert len(values_call) == 1
        link = values_call[0]
        assert link['from_unit_id'] == unit_id_1
        assert link['to_unit_id'] == str(target_uuid)
        assert link['link_type'] == 'semantic'
        assert link['weight'] == 0.9
