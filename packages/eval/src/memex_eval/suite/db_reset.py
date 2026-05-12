"""End-of-run schema wipe for the eval suite.

API-level vault deletion leaves orphan rows in ``reflection_queue``,
``audit_logs``, ``kv_entries``, ``outcome_audit_log``,
``maintenance_proposals``, and ``consolidation_ticks`` — sibling tables
that ``DELETE /vaults/<id>`` does not cascade to. The runner therefore
drops every SQLModel-managed table at end-of-run and recreates the
schema from current metadata, so the next run starts against a known
state.

Suppressed by ``--keep-vault`` and ``--reuse-vault``; the runner gates
the call on ``preserve_vaults`` before invoking us.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger('memex_eval.suite.db_reset')


def _resolve_db_dsn() -> str:
    """Resolve the Postgres DSN the suite should reset.

    Order: ``MEMEX_DATABASE_URL`` env var → MemexConfig (which itself
    reads ``MEMEX_SERVER__META_STORE__INSTANCE__*`` env vars and the
    project/global ``.memex.yaml``). YAML support is why we cannot just
    delegate to ``memex_core.storage.db_url.get_database_url`` — that
    helper is env-vars-only.
    """
    direct = os.getenv('MEMEX_DATABASE_URL')
    if direct:
        return direct
    from memex_common.config import parse_memex_config

    cfg = parse_memex_config()
    return cfg.server.meta_store.instance.connection_string


async def drop_and_recreate_schema(dsn: str | None = None) -> None:
    """Drop every SQLModel table + ``alembic_version``, then recreate.

    Steps:
      1. Terminate other Postgres backends so DROP cannot deadlock with
         the live server's connection pool. The server reconnects lazily.
      2. ``SQLModel.metadata.drop_all`` — the authoritative table list,
         per user direction ("use the sqlmodel tables to drop them all").
      3. Drop ``alembic_version`` (not in SQLModel metadata).
      4. Re-create extensions (``vector``, ``pg_trgm``, ``uuid-ossp``).
      5. ``SQLModel.metadata.create_all`` — rebuild the schema.
      6. ``alembic stamp head`` — mark migrations as applied without
         replaying them (the chain trips on already-created columns when
         applied to a baseline-built schema, same as ``memex database
         stamp head``).
    """
    # Import for side-effect: registers every model on SQLModel.metadata.
    import memex_core.memory.sql_models  # noqa: F401
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel

    resolved = dsn or _resolve_db_dsn()
    logger.info('Resetting suite DB schema via SQLModel.metadata (drop_all + create_all)')

    engine = create_async_engine(resolved, future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname = current_database() AND pid <> pg_backend_pid()'
                )
            )
            await conn.run_sync(SQLModel.metadata.drop_all)
            await conn.execute(text('DROP TABLE IF EXISTS alembic_version CASCADE'))
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
            await conn.run_sync(SQLModel.metadata.create_all)
    finally:
        await engine.dispose()

    from alembic import command

    from memex_core.migration import _alembic_cfg

    cfg = _alembic_cfg()
    # Alembic's env.py reads MEMEX_DATABASE_URL or builds from
    # MEMEX_SERVER__META_STORE__INSTANCE__*. If we resolved from YAML,
    # neither is set — pass the DSN via the ini fallback so env.py's
    # _resolve_url() picks it up when get_database_url() raises.
    cfg.set_main_option('sqlalchemy.url', resolved)
    prior_env = os.environ.get('MEMEX_DATABASE_URL')
    os.environ['MEMEX_DATABASE_URL'] = resolved
    try:
        await asyncio.to_thread(command.stamp, cfg, 'head')
    finally:
        if prior_env is None:
            os.environ.pop('MEMEX_DATABASE_URL', None)
        else:
            os.environ['MEMEX_DATABASE_URL'] = prior_env
