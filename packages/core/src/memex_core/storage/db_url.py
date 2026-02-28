"""Resolve the database URL from Memex config or environment variables."""

import os


def get_database_url() -> str:
    """Resolve the async database URL for Alembic / programmatic migrations.

    Priority:
    1. ``MEMEX_DATABASE_URL`` env var (simple override for CI / containers).
    2. Build from standard Memex ``MEMEX_SERVER__META_STORE__INSTANCE__*`` vars.
    3. Raises ``RuntimeError`` if nothing is configured.
    """
    # 1. Direct URL override
    url = os.getenv('MEMEX_DATABASE_URL')
    if url:
        return url

    # 2. Build from Memex env vars
    host = os.getenv('MEMEX_SERVER__META_STORE__INSTANCE__HOST')
    if host:
        port = os.getenv('MEMEX_SERVER__META_STORE__INSTANCE__PORT', '5432')
        database = os.getenv('MEMEX_SERVER__META_STORE__INSTANCE__DATABASE', 'postgres')
        user = os.getenv('MEMEX_SERVER__META_STORE__INSTANCE__USER', 'postgres')
        password = os.getenv('MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD', 'postgres')
        return f'postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}'

    raise RuntimeError(
        'No database URL configured. Set MEMEX_DATABASE_URL or '
        'MEMEX_SERVER__META_STORE__INSTANCE__HOST environment variable.'
    )
