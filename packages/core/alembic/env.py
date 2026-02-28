"""Alembic async migration environment for Memex.

Supports:
- Async SQLAlchemy engine (asyncpg)
- SQLModel metadata (pgvector Vector columns)
- Advisory locking to prevent concurrent migration races
"""

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

# Import all models so SQLModel.metadata is fully populated.
import memex_core.memory.sql_models  # noqa: F401
from memex_core.storage.db_url import get_database_url

logger = logging.getLogger('alembic.env')

# Alembic Config object — provides access to alembic.ini values.
config = context.config

# Interpret the config file for Python logging (if present).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata object used for autogenerate support.
target_metadata = SQLModel.metadata

# Advisory lock ID used to serialize migrations across workers.
MIGRATION_LOCK_ID = 720_701


def _resolve_url() -> str:
    """Resolve the database URL, with alembic.ini as final fallback."""
    try:
        return get_database_url()
    except RuntimeError:
        ini_url = config.get_main_option('sqlalchemy.url')
        if ini_url:
            return ini_url
        raise


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping engine
    creation we don't even need a DBAPI to be available.
    """
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    """Run migrations with an active connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with an async engine.

    Acquires a PostgreSQL advisory lock first to prevent concurrent
    migration execution across multiple workers.
    """
    connectable = create_async_engine(
        _resolve_url(),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        # Acquire advisory lock to serialize migrations
        await connection.execute(text(f'SELECT pg_advisory_lock({MIGRATION_LOCK_ID})'))
        try:
            await connection.run_sync(do_run_migrations)
        finally:
            await connection.execute(text(f'SELECT pg_advisory_unlock({MIGRATION_LOCK_ID})'))

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations — delegates to async runner."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
