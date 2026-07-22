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
from unittest.mock import AsyncMock, MagicMock, patch
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
# LLM mocking — make the suite hermetic (no LLM API key required)
# ---------------------------------------------------------------------------
#
# These tests exercise plugin <-> server <-> Postgres wiring, not LLM behaviour.
# Without a mock, ``ingestion._process_chunk`` calls ``self.memory.retain``,
# which fans out to ``run_dspy_operation`` for fact extraction; with no API
# key the call raises ``litellm.AuthenticationError``, the AsyncTransaction
# rolls back, and the note never persists. The CI ``llm-tests`` job is gated
# off (no key in secrets), so the only sustainable fix is to mock the LLM.
#
# Mirrors the unit-test ``MockDspyLM`` pattern (packages/core/tests/unit/conftest.py
# and packages/core/tests/fixtures/llm_mocks.py): patches ``run_dspy_operation``
# at the definition site AND at every module that did
# ``from memex_core.llm import run_dspy_operation`` (the function reference is
# captured at import time, so patching only the source is not enough).


_RUN_DSPY_IMPORT_SITES: tuple[str, ...] = (
    'memex_core.llm.run_dspy_operation',
    'memex_core.memory.extraction.core.run_dspy_operation',
    # Note: as of F25b the classifier no longer issues its own LLM call —
    # intent_class / risk_class are emitted directly by the extraction
    # signature, so ``classifier.run_dspy_operation`` no longer exists.
    'memex_core.memory.reflect.reflection.run_dspy_operation',
    'memex_core.memory.retrieval.expansion.run_dspy_operation',
    'memex_core.memory.retrieval.temporal_concretizer.run_dspy_operation',
    'memex_core.memory.contradiction.engine.run_dspy_operation',
    'memex_core.processing.titles.run_dspy_operation',
    'memex_core.processing.dates.run_dspy_operation',
    'memex_core.services.search.run_dspy_operation',
    'memex_core.services.vault_summary.run_dspy_operation',
)


def _make_empty_extraction_result() -> MagicMock:
    """Default mock result: an ``ExtractedOutput`` with zero facts.

    Shape mirrors what ``_extract_facts_from_chunk`` and
    ``extract_facts_from_frontmatter`` consume::

        result.extracted_facts.extracted_facts -> []

    Other call sites read different attributes (``pred.detected_headers``,
    ``result.classifier`` etc.); ``MagicMock`` returns child mocks for those
    by default, which is harmless for the wiring tests we care about — they
    only hit the simple-strategy extraction path with meaningful titles, so
    no LLM-derived field is ever asserted on.
    """
    result = MagicMock()
    result.extracted_facts.extracted_facts = []
    return result


async def _mock_run_dspy(*args: Any, **kwargs: Any) -> Any:
    return _make_empty_extraction_result()


@pytest.fixture(scope='session', autouse=True)
def _mock_run_dspy_operation_for_hermes_integration() -> Generator[None, None, None]:
    """Patch ``run_dspy_operation`` for the entire integration session.

    Session-scoped + autouse so the patch is installed BEFORE the uvicorn
    server thread starts (the server runs in-process, so module-level
    patches are visible across threads). Wrapping ``AsyncMock`` per-site
    keeps each ``patch`` independent — necessary because some of the call
    sites import the symbol via ``from ... import run_dspy_operation``,
    binding the reference at import time.
    """
    patches = [
        patch(target, new=AsyncMock(side_effect=_mock_run_dspy))
        for target in _RUN_DSPY_IMPORT_SITES
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


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
    # Disable the NLI polarity gate: these wiring tests never exercise lint_llm,
    # and leaving it on makes the lifespan eager-warm the NLI model — a ~250MB
    # HuggingFace download inside the server-boot deadline (issue #267). Off, the
    # lifespan skips NLI warmup entirely and the boot path carries no NLI fetch.
    os.environ['MEMEX_SERVER__MEMORY__LINT_LLM__POLARITY__ENABLED'] = 'false'


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


@pytest_asyncio.fixture(scope='session')
async def _models_prefetched(_schema_ready: None) -> None:
    """Download the ONNX models the lifespan warms (embedding / reranker / NER /
    NLI) BEFORE the server boots, on the main thread with no deadline.

    The server's lifespan warmup loads these models, and on a fresh runner the
    first-time Hugging Face download of ~200MB+ runs *inside* the fixture's
    startup deadline. When it overruns, ``server.started`` never flips and every
    test errors with a bare ``Memex test server failed to start`` (issue #267).

    Fetching them here moves the unbounded download out of the timed boot
    window and primes the in-process module caches (the server runs
    in-process), so warmup is fast and deterministic. Uses the same loaders and
    config the lifespan uses, so cache paths and revisions match exactly. The
    set MUST mirror ``lifespan()`` in ``memex_core.server`` — any model warmed
    there but skipped here would still cold-download inside the deadline.

    Depends on ``_schema_ready`` only to guarantee ``_set_memex_env_vars`` has
    run first (``MEMEX_LOAD_LOCAL_CONFIG``/``MEMEX_LOAD_GLOBAL_CONFIG=false``),
    so ``parse_memex_config`` here resolves the same backend the lifespan will.
    """
    from memex_core.config import parse_memex_config
    from memex_core.memory.models.embedding import get_embedding_model
    from memex_core.memory.models.ner import get_ner_model
    from memex_core.memory.models.nli import get_nli_model
    from memex_core.memory.models.reranking import get_reranking_model

    config = parse_memex_config()
    await get_embedding_model(
        config.server.embedding_model,
        batch_size=config.server.embedding_batch_size,
    )
    await get_reranking_model(
        config.server.memory.retrieval.reranker,
        batch_size=config.server.memory.retrieval.reranker_batch_size,
    )
    await get_ner_model()
    # NLI is eager-warmed by the lifespan only when the polarity gate is enabled.
    # _set_memex_env_vars turns it OFF for this suite, so this branch is normally
    # skipped. It stays gated on the same flag (mirroring lifespan) so that if a
    # test ever re-enables polarity, the NLI download still happens here, outside
    # the boot deadline. Best-effort like the lifespan's warmup (nli.py's tokenizer
    # load can fail): a prefetch failure must not error every test at setup.
    if config.server.memory.lint_llm.polarity.enabled:
        try:
            await get_nli_model(config.server.memory.lint_llm.polarity)
        except Exception:  # noqa: BLE001 — mirrors lifespan best-effort warmup
            pass


@pytest.fixture(scope='session')
def memex_server_url(
    postgres_container: Any,
    _schema_ready: None,
    _models_prefetched: None,
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

    # Capture any exception raised during lifespan startup. Without this, a real
    # startup crash is invisible: uvicorn runs at log_level='warning' on a daemon
    # thread, so the fixture only sees ``server.started`` never flipping and
    # raises a bare timeout that hides the actual cause (issue #267).
    startup_error: dict[str, BaseException] = {}

    def _run_server() -> None:
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 — surface the real cause
            startup_error['exc'] = exc

    thread = threading.Thread(target=_run_server, daemon=True, name='memex-test-uvicorn')
    thread.start()

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if server.started or 'exc' in startup_error:
            break
        time.sleep(0.05)
    if not server.started:
        scheduler_patch.stop()
        if 'exc' in startup_error:
            raise RuntimeError('Memex test server failed to start') from startup_error['exc']
        raise RuntimeError(
            'Memex test server did not report started within 60s (no exception '
            'was raised on the server thread). If the log shows an in-progress '
            'model download, a model the lifespan warms is missing from the '
            '_models_prefetched fixture (it must mirror lifespan()).'
        )

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


def _ensure_quality_gate_disabled(hermes_home: Path) -> None:
    """Write or merge quality-gate-off config so short test turns pass."""
    import json

    cfg_path = hermes_home / 'memex' / 'config.json'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    existing.setdefault('retain', {}).update(
        {
            'min_capture_turns': 0,
            'min_capture_chars': 0,
        }
    )
    cfg_path.write_text(json.dumps(existing))


@pytest.fixture(autouse=True)
def _hermes_env(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('HERMES_HOME', str(hermes_home))
    _ensure_quality_gate_disabled(hermes_home)


# ---------------------------------------------------------------------------
# Plugin lifecycle helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_name() -> str:
    return f'hermes-int-{uuid4().hex[:8]}'


@pytest_asyncio.fixture
async def live_vault(live_api: Any, vault_name: str) -> UUID:
    vault = await live_api.create_vault(name=vault_name)
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
