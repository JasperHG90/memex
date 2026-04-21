"""Hermes integration fixtures — real Hermes loader × live Memex server × Postgres.

The suite starts Memex's FastAPI app under ``uvicorn`` on a free port in a
background thread, backed by a testcontainers-managed
``pgvector/pgvector:pg18-trixie`` Postgres. The plugin talks to the server
over real HTTP — matching production — which also keeps the plugin's async
bridge loop isolated from pytest-asyncio's loop (asyncpg connections are
bound to the loop they're created on).

Requirements:
- Docker daemon running (for testcontainers)
- ``hermes-agent`` installed (``uv sync --group hermes-integration``)

Run with:

    just test-integration

Suite is gated by the ``hermes_integration`` pytest marker and excluded from
the default run so unit tests stay fast.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, AsyncGenerator, Generator, Iterator
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

_HERMES_INSTALL_HINT = (
    'hermes-agent is not installed. Integration tests need it. Install with:\n'
    '    uv sync --group hermes-integration\n'
    'then run ``just test-integration``.'
)


# ---------------------------------------------------------------------------
# Skip the entire suite early if Docker or hermes-agent are missing
# ---------------------------------------------------------------------------


def _hermes_is_importable() -> bool:
    import importlib.util

    return all(
        importlib.util.find_spec(m) is not None
        for m in ('hermes_constants', 'agent.memory_provider', 'tools.registry', 'plugins.memory')
    )


def _docker_is_available() -> bool:
    try:
        import docker  # type: ignore[import-not-found,attr-defined]

        docker.from_env().ping()  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


@pytest.fixture(scope='session', autouse=True)
def _require_prerequisites() -> None:
    if not _hermes_is_importable():
        pytest.skip(_HERMES_INSTALL_HINT, allow_module_level=False)
    if not _docker_is_available():
        pytest.skip(
            'Docker daemon not reachable. Integration tests need Docker for '
            'testcontainers Postgres.',
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Postgres + Memex app (mirrors tests/conftest.py patterns)
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session')
def postgres_container() -> Generator[Any, None, None]:
    from testcontainers.postgres import PostgresContainer

    pg = PostgresContainer('pgvector/pgvector:pg18-trixie')
    pg.start()
    try:
        yield pg
    finally:
        pg.stop()


def _set_memex_env_vars(pg: Any) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(pg.get_connection_url())
    os.environ['MEMEX_LOAD_LOCAL_CONFIG'] = 'false'
    os.environ['MEMEX_LOAD_GLOBAL_CONFIG'] = 'false'
    os.environ['MEMEX_SERVER__META_STORE__TYPE'] = 'postgres'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__HOST'] = parsed.hostname or 'localhost'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PORT'] = str(parsed.port or 5432)
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__DATABASE'] = parsed.path.lstrip('/')
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__USER'] = parsed.username or 'test'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD'] = parsed.password or 'test'
    os.environ['MEMEX_SERVER__MEMORY__REFLECTION__BACKGROUND_REFLECTION_ENABLED'] = 'false'


@pytest_asyncio.fixture(scope='session')
async def _schema_ready(postgres_container: Any) -> AsyncGenerator[None, None]:
    """Initialize the DB schema + seed the global vault (session-scoped)."""
    _set_memex_env_vars(postgres_container)

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlmodel import SQLModel

    from memex_core.config import GLOBAL_VAULT_ID, GLOBAL_VAULT_NAME
    from memex_core.memory.sql_models import Vault
    from memex_core.migration import get_expected_head

    dsn = postgres_container.get_connection_url().replace('psycopg2', 'asyncpg')
    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        await conn.run_sync(SQLModel.metadata.create_all)
        await conn.execute(
            text(
                'CREATE TABLE IF NOT EXISTS alembic_version ('
                '  version_num VARCHAR(32) NOT NULL PRIMARY KEY)'
            )
        )
        await conn.execute(text('DELETE FROM alembic_version'))
        await conn.execute(
            text('INSERT INTO alembic_version (version_num) VALUES (:rev)'),
            {'rev': get_expected_head()},
        )

    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_maker() as s:
        s.add(
            Vault(
                id=GLOBAL_VAULT_ID,
                name=GLOBAL_VAULT_NAME,
                description='Hermes integration global vault',
            )
        )
        await s.commit()
    await engine.dispose()
    yield


@pytest.fixture(scope='session')
def memex_server_url(
    postgres_container: Any,
    _schema_ready: None,
) -> Generator[str, None, None]:
    """Run Memex FastAPI in a background thread with uvicorn. Yield base URL.

    We can't use ``ASGITransport`` because the plugin's ``async_bridge`` runs
    on its own event loop; asyncpg connections created on the server loop can't
    be reused across loops. A real HTTP server isolates the two.
    """
    import socket
    import threading
    import time

    import uvicorn

    from memex_core.server import app

    # Patch the background scheduler so it doesn't contend for Postgres locks.
    import asyncio as _asyncio

    async def _noop(*_a: Any, **_kw: Any) -> None:
        await _asyncio.Event().wait()

    scheduler_patch = patch(
        'memex_core.server.run_scheduler_with_leader_election', side_effect=_noop
    )
    scheduler_patch.start()

    # Find a free port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]

    class _Server(uvicorn.Server):
        def install_signal_handlers(self) -> None:  # type: ignore[override]
            pass

    config = uvicorn.Config(
        app=app, host='127.0.0.1', port=port, log_level='warning', lifespan='on'
    )
    server = _Server(config=config)
    thread = threading.Thread(target=server.run, daemon=True, name='memex-test-uvicorn')
    thread.start()

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        scheduler_patch.stop()
        raise RuntimeError('Memex test server failed to start')

    url = f'http://127.0.0.1:{port}'
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        scheduler_patch.stop()


@pytest_asyncio.fixture(scope='function')
async def _fresh_db(postgres_container: Any) -> AsyncGenerator[None, None]:
    """Per-test marker fixture; no-op.

    We don't TRUNCATE between tests: the prior test's background batch jobs
    may still hold row locks, which deadlock against TRUNCATE's
    AccessExclusiveLock. Each test uses a uuid-based vault and unique query
    strings, so cross-test data is harmless.
    """
    yield


# ---------------------------------------------------------------------------
# RemoteMemexAPI wired to the in-process app via ASGITransport
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope='function')
async def live_api(memex_server_url: str, _fresh_db: None) -> AsyncGenerator[Any, None]:
    """``RemoteMemexAPI`` connected to the uvicorn test server over HTTP."""
    import httpx

    from memex_common.client import RemoteMemexAPI

    async with httpx.AsyncClient(base_url=f'{memex_server_url}/api/v1/', timeout=30.0) as client:
        yield RemoteMemexAPI(client)


@pytest.fixture(scope='function')
def server_url_env(memex_server_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Set ``MEMEX_SERVER_URL`` so the plugin connects to our test server."""
    monkeypatch.setenv('MEMEX_SERVER_URL', memex_server_url)
    return memex_server_url


# ---------------------------------------------------------------------------
# HERMES_HOME + symlinked plugin
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session')
def hermes_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp('hermes-home')


@pytest.fixture(scope='session')
def installed_plugin(hermes_home: Path) -> Path:
    source = Path(__file__).resolve().parents[2] / 'src' / 'memex_hermes_plugin' / 'memex'
    assert source.exists(), f'plugin source not found at {source}'
    plugin_dir = hermes_home / 'plugins' / 'memex'
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    if plugin_dir.exists() or plugin_dir.is_symlink():
        if plugin_dir.is_symlink() or plugin_dir.is_file():
            plugin_dir.unlink()
        else:
            import shutil

            shutil.rmtree(plugin_dir)
    plugin_dir.symlink_to(source.resolve(), target_is_directory=True)
    return plugin_dir


@pytest.fixture(autouse=True)
def _hermes_env(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('HERMES_HOME', str(hermes_home))


# ---------------------------------------------------------------------------
# Plugin lifecycle helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_name() -> str:
    return f'hermes-int-{uuid4().hex[:8]}'


@pytest_asyncio.fixture
async def live_vault(live_api: Any, vault_name: str) -> UUID:
    from memex_common.schemas import CreateVaultRequest

    vault = await live_api.create_vault(CreateVaultRequest(name=vault_name))
    return UUID(str(vault.id))


@pytest.fixture
def loaded_provider(
    installed_plugin: Path,  # noqa: ARG001 — ensures symlink
    server_url_env: str,  # noqa: ARG001 — MEMEX_SERVER_URL set
    live_vault: UUID,  # noqa: ARG001 — vault must exist
    vault_name: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[Any]:
    monkeypatch.setenv('MEMEX_VAULT', vault_name)
    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]

    caplog.set_level(logging.WARNING, logger='memex_hermes_plugin')
    provider = load_memory_provider('memex')
    if provider is None:
        pytest.fail('Hermes loader returned None for "memex"')
    yield provider
    try:
        provider.shutdown()
    except Exception:
        pass


@pytest.fixture
def initialized_provider(loaded_provider: Any, hermes_home: Path) -> Any:
    loaded_provider.initialize(
        'integration-session',
        hermes_home=str(hermes_home),
        platform='cli',
        agent_identity='integration',
        user_id='tester',
    )
    return loaded_provider
