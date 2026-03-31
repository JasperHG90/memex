import asyncio
import pytest
from unittest.mock import AsyncMock
from memex_core.config import (
    MemexConfig,
    ReflectionConfig,
    PostgresMetaStoreConfig,
    PostgresInstanceConfig,
)
from memex_core.scheduler import run_scheduler_with_leader_election, periodic_reflection_task


# Mock MemexAPI
class MockMemexAPI:
    def __init__(self):
        self.claim_reflection_queue_batch = AsyncMock(return_value=[])
        self.reflect_batch = AsyncMock(return_value=[])
        self.recover_stale_processing = AsyncMock(return_value=0)


@pytest.fixture
def mock_api():
    return MockMemexAPI()


@pytest.fixture
def mock_config():
    from memex_core.config import (
        ServerConfig,
        MemoryConfig,
        ExtractionConfig,
        ModelConfig,
    )

    config = MemexConfig(
        server=ServerConfig(
            memory=MemoryConfig(
                reflection=ReflectionConfig(
                    background_reflection_enabled=True,
                    background_reflection_interval_seconds=60,  # fast for test
                    background_reflection_batch_size=2,
                ),
                extraction=ExtractionConfig(
                    model=ModelConfig(model='gemini/gemini-3-flash-preview')
                ),
            ),
            meta_store=PostgresMetaStoreConfig(
                instance=PostgresInstanceConfig(
                    host='localhost', database='test_db', user='test', password='password'
                )
            ),
        )
    )
    return config


@pytest.mark.asyncio
async def test_scheduler_disabled_config(mock_config, mock_api):
    """Test that scheduler returns immediately if disabled."""
    mock_config.server.memory.reflection.background_reflection_enabled = False

    # Run with timeout to ensure it doesn't block
    try:
        await asyncio.wait_for(
            run_scheduler_with_leader_election(mock_config, mock_api), timeout=1.0
        )
    except asyncio.TimeoutError:
        pytest.fail('Scheduler should have returned immediately when disabled.')


@pytest.mark.asyncio
async def test_scheduler_task_execution(mock_api):
    """
    Test the task execution logic directly.
    """
    from memex_common.schemas import ReflectionQueueDTO
    from uuid import uuid4

    # 1. Setup mock data
    item1 = ReflectionQueueDTO(entity_id=uuid4(), vault_id=uuid4(), priority_score=1.0)
    mock_api.claim_reflection_queue_batch.return_value = [item1]

    # 2. Run task
    await periodic_reflection_task(mock_api, batch_size=5)

    # 3. Assertions
    mock_api.claim_reflection_queue_batch.assert_called_once_with(limit=5)
    mock_api.reflect_batch.assert_called_once()

    # Verify arguments passed to reflect_batch
    call_args = mock_api.reflect_batch.call_args[0][0]  # first arg is list of requests
    assert len(call_args) == 1
    assert call_args[0].entity_id == item1.entity_id


@pytest.mark.asyncio
async def test_scheduler_task_empty_queue(mock_api):
    """Test task when queue is empty."""
    mock_api.claim_reflection_queue_batch.return_value = []

    await periodic_reflection_task(mock_api, batch_size=5)

    mock_api.claim_reflection_queue_batch.assert_called_once()
    mock_api.reflect_batch.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_recovers_stale_before_claiming(mock_api):
    """Scheduler must call recover_stale_processing before claiming new items."""
    call_order: list[str] = []

    async def _recover() -> int:
        call_order.append('recover')
        return 0

    async def _claim(**kw: object) -> list[object]:
        call_order.append('claim')
        return []

    mock_api.recover_stale_processing.side_effect = _recover
    mock_api.claim_reflection_queue_batch.side_effect = _claim

    await periodic_reflection_task(mock_api, batch_size=5)

    assert call_order == ['recover', 'claim']
    mock_api.recover_stale_processing.assert_called_once()
