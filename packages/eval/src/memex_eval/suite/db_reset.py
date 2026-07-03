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
      6. Re-stamp ``alembic_version`` by DIRECT SQL (create table + insert
         the head revision(s)) — NOT ``alembic command.stamp``, which trips
         on this project's 128-char ``version_num`` by ALTERing the table
         before it exists. See the inline note at the INSERT.
    """
    # Import for side-effect: registers every model on SQLModel.metadata.
    import memex_core.memory.sql_models  # noqa: F401
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel

    resolved = dsn or _resolve_db_dsn()
    logger.info('Resetting suite DB schema via SQLModel.metadata (drop_all + create_all)')

    # Resolve the head revision(s) from the migration script directory up
    # front, so we can stamp via direct SQL below (see the long note before
    # the INSERT for why we don't use ``alembic command.stamp``).
    from alembic.script import ScriptDirectory

    from memex_core.migration import _alembic_cfg

    cfg = _alembic_cfg()
    cfg.set_main_option('sqlalchemy.url', resolved)
    heads = list(ScriptDirectory.from_config(cfg).get_heads())

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

            # Re-stamp the version table by DIRECT SQL — NOT ``alembic
            # command.stamp``. This project's alembic env configures a
            # 128-char ``version_num``; alembic's stamp path emits an
            # ``ALTER TABLE alembic_version ALTER COLUMN version_num TYPE
            # varchar(128)`` BEFORE the table exists, which raises
            # UndefinedTable against the freshly-wiped schema and leaves the
            # DB un-stamped — crashing the NEXT suite that reads
            # ``alembic_version`` (e.g. ``SELECT version_num``). Creating the
            # table and inserting the head revision(s) is exactly the state a
            # successful stamp produces, without the ordering bug.
            await conn.execute(
                text(
                    'CREATE TABLE IF NOT EXISTS alembic_version ('
                    'version_num VARCHAR(128) NOT NULL, '
                    'CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))'
                )
            )
            await conn.execute(text('DELETE FROM alembic_version'))
            for rev in heads:
                await conn.execute(
                    text('INSERT INTO alembic_version (version_num) VALUES (:rev)'),
                    {'rev': rev},
                )
    finally:
        await engine.dispose()
