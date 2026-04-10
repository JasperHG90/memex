import asyncio
import logging
import asyncpg
from typing import TYPE_CHECKING
from aioclock import AioClock
from aioclock.triggers import Every
from sqlalchemy.engine.url import make_url

from memex_core.config import MemexConfig
from memex_core.context import background_session

if TYPE_CHECKING:
    from memex_core.api import MemexAPI

logger = logging.getLogger('memex.core.scheduler')

# Arbitrary 64-bit integer for Postgres Advisory Lock
MEMEX_LEADER_LOCK_ID = 5432789123456789


async def periodic_reflection_task(api: 'MemexAPI', batch_size: int):
    """
    The actual business logic to run periodically.
    """
    async with background_session('bg-sched-reflect'):
        logger.info('Scheduler: Running periodic reflection check...')
        try:
            # 0. Recover stale PROCESSING items before claiming new ones
            recovered = await api.recover_stale_processing()
            if recovered:
                logger.info(f'Scheduler: Recovered {recovered} stale PROCESSING items.')

            # 1. Claim items
            queue_items = await api.claim_reflection_queue_batch(limit=batch_size)
            if not queue_items:
                return

            # 2. Trigger batch reflection
            from memex_core.memory.reflect.models import ReflectionRequest

            requests = [
                ReflectionRequest(
                    entity_id=item.entity_id,
                    vault_id=item.vault_id,
                    limit_recent_memories=20,
                )
                for item in queue_items
            ]

            logger.info(f'Scheduler: Reflecting on {len(requests)} entities.')
            await api.reflect_batch(requests)

        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Scheduler: Task failed: {e}', exc_info=True)


async def periodic_vault_summary_task(api: 'MemexAPI'):
    """Check each vault for staleness and update summaries.

    Routes to ``regenerate_summary()`` when the ``needs_regeneration`` flag
    is set (content was deleted/archived), otherwise falls through to
    ``update_summary()`` for incremental updates (new notes added).
    """
    async with background_session('bg-sched-vault-summary'):
        logger.info('Scheduler: Running vault summary check...')
        try:
            vaults = await api.list_vaults()
            for vault in vaults:
                summary = await api.vault_summary.get_summary(vault.id)
                if summary and summary.needs_regeneration:
                    logger.info(
                        f'Scheduler: Regenerating summary for vault {vault.name} '
                        '(needs_regeneration flag set)'
                    )
                    await api.vault_summary.regenerate_summary(vault.id)
                elif await api.vault_summary.is_stale(vault.id):
                    logger.info(f'Scheduler: Updating stale summary for vault {vault.name}')
                    await api.vault_summary.update_summary(vault.id)
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Scheduler: Vault summary task failed: {e}', exc_info=True)


async def periodic_kv_ttl_cleanup_task(api: 'MemexAPI'):
    """Delete expired KV entries."""
    async with background_session('bg-sched-kv-ttl'):
        try:
            count = await api.kv_cleanup_expired()
            if count:
                logger.info(f'Scheduler: Deleted {count} expired KV entries.')
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Scheduler: KV TTL cleanup failed: {e}', exc_info=True)


async def run_scheduler_with_leader_election(config: MemexConfig, api: 'MemexAPI'):
    """
    Leader election loop using Postgres Advisory Locks.
    If Leader: Starts AioClock.
    """
    if not config.server.memory.reflection.background_reflection_enabled:
        logger.info('Scheduler: Background reflection DISABLED.')
        return

    interval_seconds = config.server.memory.reflection.background_reflection_interval_seconds
    batch_size = config.server.memory.reflection.background_reflection_batch_size

    min_priority = config.server.memory.reflection.min_priority
    logger.info(
        f'Scheduler: Starting. Interval: {interval_seconds}s. Batch: {batch_size}. '
        f'Min priority: {min_priority}.'
    )

    # Define AioClock App
    clock = AioClock()

    @clock.task(trigger=Every(seconds=interval_seconds))
    async def run_reflection_job():
        await periodic_reflection_task(api, batch_size)

    # Vault summary periodic task
    if config.server.vault_summary.enabled:
        vs_interval = config.server.vault_summary.interval_seconds
        logger.info(f'Scheduler: Vault summary enabled. Interval: {vs_interval}s.')

        @clock.task(trigger=Every(seconds=vs_interval))
        async def run_vault_summary_job():
            await periodic_vault_summary_task(api)

    # KV TTL cleanup — purge expired entries every 5 minutes
    @clock.task(trigger=Every(seconds=300))
    async def run_kv_ttl_cleanup():
        await periodic_kv_ttl_cleanup_task(api)

    # asyncpg requires a plain postgresql:// DSN (no +asyncpg driver suffix)
    sa_url = make_url(config.server.meta_store.instance.connection_string)
    dsn = sa_url.set(drivername='postgresql').render_as_string(hide_password=False)

    while True:
        conn = None
        try:
            conn = await asyncpg.connect(dsn)

            # Try to acquire lock
            is_leader = await conn.fetchval('SELECT pg_try_advisory_lock($1)', MEMEX_LEADER_LOCK_ID)

            if is_leader:
                logger.info('Scheduler: Lock acquired. I am LEADER. Starting AioClock...')

                # Start AioClock
                serve_task = asyncio.create_task(clock.serve())

                try:
                    while not serve_task.done():
                        if conn.is_closed():
                            logger.error('Scheduler: Lost Postgres connection! stepping down...')
                            serve_task.cancel()
                            break
                        await asyncio.sleep(5)

                except asyncio.CancelledError:
                    serve_task.cancel()
                    raise
                finally:
                    if not serve_task.done():
                        serve_task.cancel()

                    try:
                        await serve_task
                    except asyncio.CancelledError:
                        pass

                    logger.info('Scheduler: AioClock stopped.')
                    if not conn.is_closed():
                        await conn.execute('SELECT pg_advisory_unlock($1)', MEMEX_LEADER_LOCK_ID)
                        await conn.close()
            else:
                # Follower
                await conn.close()
                await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info('Scheduler: Shutting down.')
            if conn and not conn.is_closed():
                await conn.close()
            return
        except (OSError, asyncpg.PostgresError, RuntimeError) as e:
            logger.error(f'Scheduler: Error: {e}')
            if conn and not conn.is_closed():
                await conn.close()
            await asyncio.sleep(10)
