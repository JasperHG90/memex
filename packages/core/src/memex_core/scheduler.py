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

    logger.info(f'Scheduler: Starting. Interval: {interval_seconds}s. Batch: {batch_size}.')

    # Define AioClock App
    clock = AioClock()

    @clock.task(trigger=Every(seconds=interval_seconds))
    async def run_reflection_job():
        await periodic_reflection_task(api, batch_size)

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
