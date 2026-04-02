import os
import pytest
import pytest_asyncio
import yaml
from typing import Generator, AsyncGenerator
from sqlalchemy import text, NullPool
from sqlmodel import SQLModel
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from testcontainers.postgres import PostgresContainer
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport
from memex_core.server import app
from memex_core.config import GLOBAL_VAULT_ID, GLOBAL_VAULT_NAME
from memex_core.memory.sql_models import Vault

import logging

# Start Postgres Container
postgres = PostgresContainer('pgvector/pgvector:pg18-trixie')


@pytest.fixture(autouse=True)
def _disable_background_scheduler():
    """Prevent the reflection scheduler from running during E2E tests.

    The scheduler continuously polls Postgres for advisory locks.  Tests that
    enter the lifespan via ``runner.invoke()`` + ``nest_asyncio`` can deadlock
    because the synchronous CLI runner blocks the event loop while the
    scheduler's async DB calls are pending.
    """
    from unittest.mock import patch as _patch

    async def _noop(*_a, **_kw):
        import asyncio

        await asyncio.Event().wait()

    with _patch('memex_core.server.run_scheduler_with_leader_election', side_effect=_noop):
        yield


@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    """Reset the global LLM circuit breaker before each test to prevent cross-test contamination."""
    from memex_core.llm import get_circuit_breaker

    get_circuit_breaker().reset()
    yield
    get_circuit_breaker().reset()


@pytest.fixture(scope='session')
def postgres_container() -> Generator[PostgresContainer, None, None]:
    postgres.start()
    yield postgres
    postgres.stop()


@pytest.fixture(scope='session')
def postgres_url(postgres_container: PostgresContainer) -> str:
    url = postgres_container.get_connection_url().replace('psycopg2', 'asyncpg')
    return url


@pytest.fixture(scope='function', autouse=True)
def tmp_env(tmp_path) -> Generator[None, None, None]:
    """
    Set up a temporary environment for each test.
    - config/
    - data/
    - logs/
    - config.yaml pointing to data/ and logs/
    """
    # Create directory structure
    config_dir = tmp_path / 'config'
    data_dir = tmp_path / 'data'
    logs_dir = tmp_path / 'logs'

    config_dir.mkdir()
    data_dir.mkdir()
    logs_dir.mkdir()

    log_file = logs_dir / 'memex.log'
    config_file = config_dir / 'config.yaml'

    # Create configuration content
    config_data = {
        'server': {
            'file_store': {'type': 'local', 'root': str(data_dir)},
            'meta_store': {
                'type': 'postgres',
                'instance': {
                    'host': 'localhost',
                    'database': 'memex',
                    'user': 'memex',
                    'password': 'memex',
                },
            },
            'logging': {'log_file': str(log_file), 'level': 'DEBUG'},
        }
    }

    with open(config_file, 'w') as f:
        yaml.dump(config_data, f)

    # Set Environment Variables
    old_env = os.environ.copy()
    os.environ['MEMEX_CONFIG_PATH'] = str(config_file)
    os.environ['MEMEX_LOAD_LOCAL_CONFIG'] = 'false'
    os.environ['MEMEX_LOAD_GLOBAL_CONFIG'] = 'false'
    os.environ['MEMEX_SERVER__FILE_STORE__ROOT'] = str(data_dir)
    os.environ['MEMEX_SERVER__LOGGING__LOG_FILE'] = str(log_file)

    # Redirect the root logger and the 'memex' child logger to an isolated temp
    # file for the duration of this test. Without this, log records from the
    # server lifespan (which installs its own StreamHandler on the 'memex'
    # logger) or from any logging.basicConfig call can leak into the production
    # log file at user_log_dir('memex').
    root_logger = logging.getLogger()
    memex_logger = logging.getLogger('memex')

    # Snapshot current state for both loggers
    original_root_handlers = root_logger.handlers[:]
    original_root_level = root_logger.level
    original_memex_handlers = memex_logger.handlers[:]
    original_memex_level = memex_logger.level
    original_memex_propagate = memex_logger.propagate

    # Isolate root logger → temp file only
    root_logger.handlers = []
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    )
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.DEBUG)

    # Isolate the 'memex' child logger: clear any stale handlers (e.g. the
    # StreamHandler that the server lifespan adds) and let records propagate
    # to the root logger only, so they land in the temp file above.
    memex_logger.handlers = []
    memex_logger.propagate = True
    memex_logger.setLevel(logging.DEBUG)

    yield

    try:
        # Close and remove the temp file handler before restoring state
        file_handler.close()
        root_logger.removeHandler(file_handler)

        # Restore root logger
        root_logger.handlers = original_root_handlers
        root_logger.setLevel(original_root_level)

        # Restore 'memex' logger
        memex_logger.handlers = original_memex_handlers
        memex_logger.setLevel(original_memex_level)
        memex_logger.propagate = original_memex_propagate
    finally:
        # Restore environment atomically: remove keys added during test,
        # then restore original values. Never call os.environ.clear()
        # because a mid-teardown failure would leave the env empty.
        added_keys = set(os.environ) - set(old_env)
        for key in added_keys:
            del os.environ[key]
        os.environ.update(old_env)


@pytest_asyncio.fixture(scope='session', autouse=True)
async def init_db(postgres_url: str):
    """Initialize the database schema for the test session."""
    engine = create_async_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        await conn.run_sync(SQLModel.metadata.create_all)

    # Stamp alembic_version so metastore schema check passes
    await _stamp_alembic_head(engine)

    # Initialize Global Vault
    # We need a session or just execute raw SQL. SQLModel is easier.
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from memex_core.memory.sql_models import Vault

    SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with SessionLocal() as session:
        vault = Vault(id=GLOBAL_VAULT_ID, name=GLOBAL_VAULT_NAME, description='Test Global Vault')
        session.add(vault)
        await session.commit()

    await engine.dispose()


async def _stamp_alembic_head(engine) -> None:
    """Stamp the alembic_version table at head so schema checks pass in tests."""
    from memex_core.migration import get_expected_head

    head = get_expected_head()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                'CREATE TABLE IF NOT EXISTS alembic_version ('
                '  version_num VARCHAR(32) NOT NULL, '
                '  CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)'
                ')'
            )
        )
        await conn.execute(text('DELETE FROM alembic_version'))
        await conn.execute(
            text('INSERT INTO alembic_version (version_num) VALUES (:rev)'),
            {'rev': head},
        )


@pytest_asyncio.fixture(scope='function')
async def _truncate_db(postgres_url: str) -> AsyncGenerator[None, None]:
    """Truncate all tables and re-seed the global vault before each test."""
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        conn = await session.connection()
        for table in reversed(SQLModel.metadata.sorted_tables):
            await conn.execute(text(f'TRUNCATE TABLE {table.name} CASCADE'))
        await session.commit()

        vault = Vault(id=GLOBAL_VAULT_ID, name=GLOBAL_VAULT_NAME, description='Test Global Vault')
        session.add(vault)
        await session.commit()

    await engine.dispose()
    yield


@pytest_asyncio.fixture(scope='function')
async def db_session(postgres_url: str, _truncate_db: None) -> AsyncGenerator[AsyncSession, None]:
    """Provides a clean database session for each test, with truncated tables."""
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        yield session

    await engine.dispose()


def _set_env_vars(postgres_container: PostgresContainer):
    """Helper to set env vars for MemexConfig"""
    from urllib.parse import urlparse

    dsn = postgres_container.get_connection_url()
    parsed = urlparse(dsn)

    os.environ['MEMEX_LOAD_LOCAL_CONFIG'] = 'false'
    os.environ['MEMEX_LOAD_GLOBAL_CONFIG'] = 'false'
    os.environ['MEMEX_SERVER__META_STORE__TYPE'] = 'postgres'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__HOST'] = parsed.hostname or 'localhost'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PORT'] = str(parsed.port or 5432)
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__DATABASE'] = parsed.path.lstrip('/')
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__USER'] = parsed.username or 'test'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD'] = parsed.password or 'test'
    os.environ['MEMEX_SERVER__MEMORY__REFLECTION__BACKGROUND_REFLECTION_ENABLED'] = 'false'


@pytest.fixture(scope='function', autouse=True)
def ensure_db_env_vars(postgres_container: PostgresContainer, tmp_env: None):
    """
    Ensure that the environment variables for the Postgres container are set
    for every test function. This is critical for tests that initialize
    the server app (e.g. CLI tests) which rely on os.environ to configure
    the database connection.
    Depends on tmp_env to ensure these vars are cleaned up (since tmp_env restores os.environ).
    """
    _set_env_vars(postgres_container)


@pytest.fixture(scope='function', autouse=True)
def reset_dependency_overrides():
    """
    Ensure that dependency overrides are cleared before every test.
    This prevents leaks from unit tests that mock the API (e.g., test_server.py).
    """
    app.dependency_overrides = {}
    yield
    app.dependency_overrides = {}


@pytest.fixture(scope='function', autouse=True)
def reset_auth_state():
    """
    Clear auth_config from app.state between tests to prevent
    auth configuration from leaking across tests (e.g., a test that
    enables auth should not affect subsequent tests).
    """
    if hasattr(app.state, 'auth_config'):
        del app.state.auth_config
    yield
    if hasattr(app.state, 'auth_config'):
        del app.state.auth_config


@pytest.fixture(scope='function', autouse=True)
def reset_dspy_lm():
    """
    Save and restore dspy.settings.lm around every test.

    Tests that patch dspy.LM (the class) cause MemexAPI.__init__ to call
    dspy.settings.configure(lm=<MagicMock>), mutating the global dspy
    settings object. Without this fixture, subsequent tests inherit the
    mock LM and fail with 'LM must be an instance of dspy.BaseLM'.

    We bypass the thread-ownership check by writing to main_thread_config
    directly and resetting the ownership trackers so the next test can
    become the new owner.
    """
    import sys
    import dspy  # noqa: F401 — ensure module is loaded

    # The dspy.dsp.utils.settings name is replaced with the Settings singleton,
    # so we must reach the actual module via sys.modules to access module-level globals.
    _mod = sys.modules['dspy.dsp.utils.settings']
    original_lm = _mod.main_thread_config.get('lm')
    yield
    _mod.main_thread_config['lm'] = original_lm
    _mod.config_owner_thread_id = None  # type: ignore[attr-defined]
    _mod.config_owner_async_task = None  # type: ignore[attr-defined]


@pytest.fixture(scope='function', autouse=True)
def clear_vault_cache():
    """Clear the vault resolution cache between tests."""
    from memex_core.api import _VAULT_RESOLUTION_CACHE

    _VAULT_RESOLUTION_CACHE.clear()


@pytest.fixture(scope='function')
def client(
    postgres_container: PostgresContainer, _truncate_db: None
) -> Generator[TestClient, None, None]:
    """
    Create a TestClient with environment variables configured to point
    to the test container. DB is truncated before each test.
    """
    _set_env_vars(postgres_container)
    os.environ['MEMEX_LOAD_LOCAL_CONFIG'] = 'false'
    os.environ['MEMEX_LOAD_GLOBAL_CONFIG'] = 'false'
    with TestClient(app) as c:
        yield c


@pytest_asyncio.fixture(scope='function')
async def async_client(
    postgres_container: PostgresContainer, _truncate_db: None
) -> AsyncGenerator[AsyncClient, None]:
    """
    Create an AsyncClient for interacting with the FastAPI app directly.
    DB is truncated before each test.
    """
    _set_env_vars(postgres_container)
    os.environ['MEMEX_LOAD_LOCAL_CONFIG'] = 'false'
    os.environ['MEMEX_LOAD_GLOBAL_CONFIG'] = 'false'
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        yield c
