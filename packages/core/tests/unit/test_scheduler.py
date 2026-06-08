import asyncio
import inspect
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from memex_core.config import (
    MemexConfig,
    ReflectionConfig,
    PostgresMetaStoreConfig,
    PostgresInstanceConfig,
)
from memex_core.scheduler import (
    run_scheduler_with_leader_election,
    periodic_reflection_task,
    periodic_vault_summary_task,
)


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
    from memex_core.memory.sql_models import ReflectionQueue
    from uuid import uuid4

    # 1. Setup mock data. ``claim_reflection_queue_batch`` returns
    # ``ReflectionQueue`` SQLModel rows (not the wire DTO); the scheduler
    # partitions them on ``task_type`` ('reflect' vs 'refresh_observation')
    # before dispatching, so the fixture must carry that field.
    item1 = ReflectionQueue(
        entity_id=uuid4(), vault_id=uuid4(), priority_score=1.0, task_type='reflect'
    )
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


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_vault_summary_task_calls_regenerate_when_flag_set(mock_bg_session):
    """Scheduler should call regenerate_summary() when needs_regeneration is set."""
    # Mock background_session context manager
    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    api = MagicMock()
    vault = MagicMock()
    vault.id = 'vault-1'
    vault.name = 'test-vault'
    api.list_vaults = AsyncMock(return_value=[vault])

    # Summary exists and needs_regeneration is True
    summary = MagicMock()
    summary.needs_regeneration = True
    api.vault_summary.get_summary = AsyncMock(return_value=summary)
    api.vault_summary.regenerate_summary = AsyncMock()
    api.vault_summary.update_summary = AsyncMock()
    api.vault_summary.is_stale = AsyncMock()

    await periodic_vault_summary_task(api)

    api.vault_summary.get_summary.assert_awaited_once_with('vault-1')
    api.vault_summary.regenerate_summary.assert_awaited_once_with('vault-1')
    api.vault_summary.update_summary.assert_not_awaited()
    api.vault_summary.is_stale.assert_not_awaited()


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_vault_summary_task_calls_update_when_stale_but_no_flag(mock_bg_session):
    """Scheduler should call update_summary() when stale but needs_regeneration is not set."""
    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    api = MagicMock()
    vault = MagicMock()
    vault.id = 'vault-1'
    vault.name = 'test-vault'
    api.list_vaults = AsyncMock(return_value=[vault])

    # Summary exists but needs_regeneration is False
    summary = MagicMock()
    summary.needs_regeneration = False
    api.vault_summary.get_summary = AsyncMock(return_value=summary)
    api.vault_summary.is_stale = AsyncMock(return_value=True)
    api.vault_summary.update_summary = AsyncMock()
    api.vault_summary.regenerate_summary = AsyncMock()

    await periodic_vault_summary_task(api)

    api.vault_summary.update_summary.assert_awaited_once_with('vault-1')
    api.vault_summary.regenerate_summary.assert_not_awaited()


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_vault_summary_task_skips_when_not_stale(mock_bg_session):
    """Scheduler should skip when no flag set and not stale."""
    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    api = MagicMock()
    vault = MagicMock()
    vault.id = 'vault-1'
    vault.name = 'test-vault'
    api.list_vaults = AsyncMock(return_value=[vault])

    summary = MagicMock()
    summary.needs_regeneration = False
    api.vault_summary.get_summary = AsyncMock(return_value=summary)
    api.vault_summary.is_stale = AsyncMock(return_value=False)
    api.vault_summary.update_summary = AsyncMock()
    api.vault_summary.regenerate_summary = AsyncMock()

    await periodic_vault_summary_task(api)

    api.vault_summary.update_summary.assert_not_awaited()
    api.vault_summary.regenerate_summary.assert_not_awaited()


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_vault_summary_task_no_summary_falls_through_to_is_stale(mock_bg_session):
    """When no summary exists, fall through to is_stale() check."""
    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    api = MagicMock()
    vault = MagicMock()
    vault.id = 'vault-1'
    vault.name = 'test-vault'
    api.list_vaults = AsyncMock(return_value=[vault])

    api.vault_summary.get_summary = AsyncMock(return_value=None)
    api.vault_summary.is_stale = AsyncMock(return_value=True)
    api.vault_summary.update_summary = AsyncMock()
    api.vault_summary.regenerate_summary = AsyncMock()

    await periodic_vault_summary_task(api)

    api.vault_summary.update_summary.assert_awaited_once_with('vault-1')
    api.vault_summary.regenerate_summary.assert_not_awaited()


# ---------------------------------------------------------------------------
# periodic_diagnostics_refresh_task — error-handling branches (Hermes round-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_diagnostics_refresh_continues_after_per_vault_unexpected_exception(
    mock_bg_session,
):
    """Per-vault inner loop must continue when one vault raises an unexpected
    programming error (AttributeError / NameError) so other vaults still
    refresh this tick."""
    from memex_core.scheduler import periodic_diagnostics_refresh_task

    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    vault_a = MagicMock()
    vault_a.id = 'vault-a'
    vault_a.name = 'alpha'
    vault_b = MagicMock()
    vault_b.id = 'vault-b'
    vault_b.name = 'beta'
    vault_c = MagicMock()
    vault_c.id = 'vault-c'
    vault_c.name = 'gamma'

    api = MagicMock()
    api.list_vaults = AsyncMock(return_value=[vault_a, vault_b, vault_c])

    visited: list[str] = []

    async def _compute(vault_id, *, force_refresh=False):
        visited.append(vault_id)
        if vault_id == 'vault-b':
            raise AttributeError("'NoneType' has no attribute 'foo'")
        return ('computing', {'task_id': 't'})

    api.diagnostics = MagicMock()
    api.diagnostics.get_or_compute_manifold = AsyncMock(side_effect=_compute)

    await periodic_diagnostics_refresh_task(api)

    assert visited == ['vault-a', 'vault-b', 'vault-c'], (
        'inner loop must continue after AttributeError on vault-b'
    )


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_diagnostics_refresh_continues_after_per_vault_name_error(mock_bg_session):
    """NameError in one vault must not stop the loop."""
    from memex_core.scheduler import periodic_diagnostics_refresh_task

    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    vault_a = MagicMock(id='vault-a', name='alpha')
    vault_b = MagicMock(id='vault-b', name='beta')

    api = MagicMock()
    api.list_vaults = AsyncMock(return_value=[vault_a, vault_b])

    visited: list[str] = []

    async def _compute(vault_id, *, force_refresh=False):
        visited.append(vault_id)
        if vault_id == 'vault-a':
            raise NameError('undefined_symbol')
        return ('computing', {'task_id': 't'})

    api.diagnostics = MagicMock()
    api.diagnostics.get_or_compute_manifold = AsyncMock(side_effect=_compute)

    await periodic_diagnostics_refresh_task(api)

    assert visited == ['vault-a', 'vault-b']


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_diagnostics_refresh_outer_unexpected_exception_reraises(mock_bg_session):
    """Outer-level unexpected exception (e.g. ``api.list_vaults`` raising an
    AttributeError) must re-raise so the AioClock supervisor surfaces it,
    rather than being silently swallowed."""
    from memex_core.scheduler import periodic_diagnostics_refresh_task

    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    api = MagicMock()
    api.list_vaults = AsyncMock(side_effect=AttributeError('api missing attribute'))

    with pytest.raises(AttributeError, match='api missing attribute'):
        await periodic_diagnostics_refresh_task(api)


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_diagnostics_refresh_outer_known_exception_does_not_reraise(
    mock_bg_session,
):
    """Outer-level *known* infrastructure exceptions (OSError/RuntimeError/
    ValueError) are logged but NOT re-raised — they're treated as transient."""
    from memex_core.scheduler import periodic_diagnostics_refresh_task

    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    api = MagicMock()
    api.list_vaults = AsyncMock(side_effect=OSError('postgres unreachable'))

    # Should NOT raise.
    await periodic_diagnostics_refresh_task(api)


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_diagnostics_refresh_per_vault_known_exception_continues(
    mock_bg_session,
):
    """Per-vault OSError/RuntimeError/ValueError logs a warning but continues."""
    from memex_core.scheduler import periodic_diagnostics_refresh_task

    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    vault_a = MagicMock(id='vault-a', name='alpha')
    vault_b = MagicMock(id='vault-b', name='beta')

    api = MagicMock()
    api.list_vaults = AsyncMock(return_value=[vault_a, vault_b])

    visited: list[str] = []

    async def _compute(vault_id, *, force_refresh=False):
        visited.append(vault_id)
        if vault_id == 'vault-a':
            raise OSError('disk full')
        return ('computing', {'task_id': 't'})

    api.diagnostics = MagicMock()
    api.diagnostics.get_or_compute_manifold = AsyncMock(side_effect=_compute)

    await periodic_diagnostics_refresh_task(api)

    assert visited == ['vault-a', 'vault-b']


def test_no_inbox_router_job_registered() -> None:
    """V6 removed the in-core router: the scheduler must define no inbox task and
    register no inbox job. Guards against a stray re-introduction."""
    import memex_core.scheduler as scheduler

    assert not hasattr(scheduler, 'periodic_inbox_router_task')
    src = inspect.getsource(scheduler)
    assert 'inbox_router' not in src
    assert 'run_inbox_router_job' not in src


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_vault_summary_task_includes_system_vaults_with_summarize_override(
    mock_bg_session,
) -> None:
    """A system vault with ``policy.summarize=True`` MUST be summarised.

    Regression guard: the V11 L2 review pass initially set the summary
    scheduler to ``list_vaults(include_system=False)`` — that excluded
    system vaults from the loop entirely, breaking the contract that a
    system vault can opt into summary via its policy. The fix is
    ``include_system=True`` plus the in-loop ``summarize_enabled`` check.
    """
    from memex_core.memory.sql_models import Vault

    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    system_vault = MagicMock(spec=Vault)
    system_vault.id = 'system-vault-1'
    system_vault.name = 'case-vault'
    system_vault.kind = 'system'
    system_vault.policy = {'summarize': True}

    api = MagicMock()
    api.list_vaults = AsyncMock(return_value=[system_vault])

    summary = MagicMock()
    summary.needs_regeneration = True
    api.vault_summary.get_summary = AsyncMock(return_value=summary)
    api.vault_summary.regenerate_summary = AsyncMock()
    api.vault_summary.update_summary = AsyncMock()
    api.vault_summary.is_stale = AsyncMock()

    await periodic_vault_summary_task(api)

    # The system vault was passed to list_vaults(include_system=True) and
    # its policy override is honored — the summary path runs.
    api.list_vaults.assert_awaited_once_with(include_system=True)
    api.vault_summary.regenerate_summary.assert_awaited_once_with('system-vault-1')


@pytest.mark.asyncio
@patch('memex_core.scheduler.background_session')
async def test_vault_summary_task_skips_system_vault_without_summarize_override(
    mock_bg_session,
) -> None:
    """A system vault without ``policy.summarize=True`` MUST be skipped.

    Companion to the override test: with no policy override, the kind
    default (False) wins and the summary path is not entered.
    """
    from memex_core.memory.sql_models import Vault

    mock_bg_session.return_value.__aenter__ = AsyncMock(return_value='test-session')
    mock_bg_session.return_value.__aexit__ = AsyncMock(return_value=False)

    system_vault = MagicMock(spec=Vault)
    system_vault.id = 'system-vault-1'
    system_vault.name = 'inbox'
    system_vault.kind = 'system'
    system_vault.policy = {}  # no override — default is off

    api = MagicMock()
    api.list_vaults = AsyncMock(return_value=[system_vault])
    api.vault_summary.get_summary = AsyncMock()
    api.vault_summary.regenerate_summary = AsyncMock()
    api.vault_summary.update_summary = AsyncMock()
    api.vault_summary.is_stale = AsyncMock()

    await periodic_vault_summary_task(api)

    api.list_vaults.assert_awaited_once_with(include_system=True)
    api.vault_summary.get_summary.assert_not_awaited()
    api.vault_summary.regenerate_summary.assert_not_awaited()
    api.vault_summary.update_summary.assert_not_awaited()
