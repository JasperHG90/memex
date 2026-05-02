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


async def periodic_diagnostics_refresh_task(api: 'MemexAPI'):
    """F32: weekly UMAP manifold refresh per vault under leader lock."""
    async with background_session('bg-sched-diagnostics-refresh'):
        try:
            vaults = await api.list_vaults()
            for vault in vaults:
                try:
                    await api.diagnostics.get_or_compute_manifold(vault.id, force_refresh=True)
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning(
                        'Scheduler: Manifold compute failed for vault %s: %s',
                        vault.name,
                        e,
                    )
                except Exception:
                    # Programming errors (AttributeError/TypeError/NameError) must not
                    # silently poison the per-vault loop — log with full traceback and
                    # continue so other vaults still get refreshed this tick.
                    # ``SystemExit`` / ``KeyboardInterrupt`` / ``asyncio.CancelledError``
                    # are ``BaseException`` subclasses (not ``Exception``) and correctly
                    # escape this handler so shutdown / cancellation still propagate.
                    logger.exception(
                        'Scheduler: Unexpected error refreshing manifold for vault %s',
                        vault.name,
                    )
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Scheduler: Diagnostics refresh failed: {e}', exc_info=True)
        except Exception:
            # Programming errors at the outer level (e.g. attribute missing on api)
            # are real bugs — log the full traceback then re-raise so AioClock's
            # supervisor surfaces the failure rather than silently swallowing it.
            logger.exception('Scheduler: Unexpected fatal error in diagnostics refresh task')
            raise


async def periodic_lint_task(api: 'MemexAPI'):
    """F6: per-vault lint run under existing MEMEX_LEADER_LOCK_ID."""
    async with background_session('bg-sched-lint'):
        try:
            vaults = await api.list_vaults()
            for vault in vaults:
                try:
                    summary = await api.lint.run_rules(vault.id)
                    if summary.total_findings:
                        logger.info(
                            'Scheduler: F6 lint emitted %d findings in vault %s',
                            summary.total_findings,
                            vault.name,
                        )
                except Exception as e:
                    logger.warning(f'Scheduler: Lint run failed for vault {vault.name}: {e}')
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Scheduler: F6 lint task failed: {e}', exc_info=True)


async def periodic_consolidation_task(api: 'MemexAPI', units_per_tick: int):
    """F38: per-vault consolidation tick under the single leader lock.

    Sequential per-vault iteration — per-vault parallelism is intentionally
    out of scope (Wave 0: only F9 introduces additional advisory locks).
    Per-vault failures are warning-logged and never raise — one bad vault
    must not stop other vaults from being consolidated this tick.
    """
    async with background_session('bg-sched-consolidation'):
        try:
            vaults = await api.list_vaults()
            for vault in vaults:
                try:
                    await api.consolidation.tick(vault.id, budget=units_per_tick)
                except Exception as e:
                    logger.warning(
                        f'Scheduler: Consolidation tick failed for vault {vault.name}: {e}'
                    )
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Scheduler: Consolidation failed: {e}', exc_info=True)


async def periodic_revisit_task(api: 'MemexAPI'):
    """F20: per-vault revisit-schedule populator under existing MEMEX_LEADER_LOCK_ID.

    Walks every vault and seeds `revisit_due_at` for never-evaluated eligible
    units. Idempotent — already-scheduled units and previously-evaluated-and-
    excluded units are skipped (re-eligibility flows through this same path
    on the next tick once the eligibility predicate flips).

    Per-vault failures are warning-logged and never raise — one bad vault
    must not stop other vaults from being seeded this tick.
    """
    async with background_session('bg-sched-revisit'):
        try:
            vaults = await api.list_vaults()
            for vault in vaults:
                try:
                    scheduled = await api.revisit.populate_initial_schedules(vault.id)
                    if scheduled:
                        logger.info(
                            'Scheduler: F20 revisit scheduled %d unit(s) in vault %s',
                            scheduled,
                            vault.name,
                        )
                except Exception as e:
                    logger.warning(
                        f'Scheduler: F20 revisit populator failed for vault {vault.name}: {e}'
                    )
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Scheduler: F20 revisit task failed: {e}', exc_info=True)


async def periodic_lint_llm_task(api: 'MemexAPI'):
    """F10: per-vault surprise-gated LLM lint under MEMEX_LEADER_LOCK_ID.

    Runs the enabled DSPy lint signatures per RFC-006 Option (A): semantic
    contradiction + schema drift. Both share the same 24h cost cap, so the
    second pass eats from whatever the first one left.
    """
    from memex_core.memory.lint_llm.checks import (
        make_schema_drift_check,
        make_semantic_contradiction_check,
    )

    settings = api.config.server.memory.lint_llm
    if not settings.enabled or settings.cost_cap_per_24h <= 0:
        return

    checks: list[tuple[str, object]] = []
    if settings.checks.semantic_contradiction.enabled:
        checks.append(
            (
                'semantic_contradiction',
                make_semantic_contradiction_check(api.lm, k=settings.surprise_k),
            )
        )
    if settings.checks.schema_drift.enabled:
        checks.append(('schema_drift', make_schema_drift_check(api.lm, k=settings.surprise_k)))

    if not checks:
        logger.info('Scheduler: F10 lint_llm — no checks enabled, skipping tick')
        return

    async with background_session('bg-sched-lint-llm'):
        try:
            vaults = await api.list_vaults()
            for vault in vaults:
                for check_name, check in checks:
                    try:
                        summary = await api.lint_llm.tick(vault.id, run_llm_check=check)
                        if (
                            summary.findings_emitted
                            or summary.deferred
                            or summary.deferred_processed
                        ):
                            logger.info(
                                'Scheduler: F10 lint_llm[%s] vault=%s: '
                                'evaluated=%d emitted=%d deferred=%d processed_deferred=%d',
                                check_name,
                                vault.name,
                                summary.candidates_evaluated,
                                summary.findings_emitted,
                                summary.deferred,
                                summary.deferred_processed,
                            )
                    except Exception as e:
                        logger.warning(
                            'Scheduler: F10 lint_llm[%s] failed for vault %s: %s',
                            check_name,
                            vault.name,
                            e,
                        )
        except (OSError, RuntimeError, ValueError) as e:
            logger.error(f'Scheduler: F10 lint_llm task failed: {e}', exc_info=True)


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

    # ============================================================
    # Tier A — Scheduler tasks (under MEMEX_LEADER_LOCK_ID)
    # F6 lint task:                WS-linter
    # F20 revisit seed task:       WS-revisit
    # F32 diagnostics refresh:     WS-diagnostics
    # F38 consolidation tick:      WS-quick-wins
    # ============================================================

    # --- F6 lint ---       (filled by WS-linter)
    if config.server.memory.lint.enabled:
        lint_interval = config.server.memory.lint.interval_seconds

        @clock.task(trigger=Every(seconds=lint_interval))
        async def run_lint_job():
            await periodic_lint_task(api)

    # --- F10 lint_llm ---  (filled by WS-linter)
    lint_llm_cfg = config.server.memory.lint_llm
    if lint_llm_cfg.enabled and lint_llm_cfg.cost_cap_per_24h > 0:
        logger.info(
            f'Scheduler: F10 lint_llm enabled. '
            f'Interval: {lint_llm_cfg.interval_seconds}s. '
            f'Cap: {lint_llm_cfg.cost_cap_per_24h}/24h/vault. '
            f'Threshold: {lint_llm_cfg.surprise_threshold}.'
        )

        @clock.task(trigger=Every(seconds=lint_llm_cfg.interval_seconds))
        async def run_lint_llm_job():
            await periodic_lint_llm_task(api)
    else:
        logger.info('Scheduler: F10 lint_llm DISABLED (enabled=False or cost_cap_per_24h=0).')

    # --- F20 revisit ---   (filled by WS-revisit)
    if config.server.memory.revisit.enabled:
        revisit_interval = config.server.memory.revisit.interval_seconds

        @clock.task(trigger=Every(seconds=revisit_interval))
        async def run_revisit_job():
            await periodic_revisit_task(api)

    # --- F32 diagnostics --- (filled by WS-diagnostics)
    @clock.task(trigger=Every(seconds=7 * 86400))
    async def run_diagnostics_refresh():
        await periodic_diagnostics_refresh_task(api)

    # --- F38 consolidation --- (filled by WS-quick-wins)
    consolidation_cfg = config.server.memory.consolidation
    if consolidation_cfg.enabled:
        logger.info(
            f'Scheduler: F38 consolidation enabled. '
            f'Cadence: {consolidation_cfg.cadence_seconds}s. '
            f'Budget: {consolidation_cfg.units_per_tick} units/tick.'
        )

        @clock.task(trigger=Every(seconds=consolidation_cfg.cadence_seconds))
        async def run_consolidation_job():
            await periodic_consolidation_task(api, consolidation_cfg.units_per_tick)
    else:
        logger.info('Scheduler: F38 consolidation DISABLED via config.')

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
