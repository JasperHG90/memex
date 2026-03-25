import asyncio
import logging
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from memex_core.api import MemexAPI
from memex_core.config import parse_memex_config
from memex_common.exceptions import MemexError
from memex_common.config import (
    GLOBAL_VAULT_NAME,
    LocalYamlConfigSettingsSource,
    MemexConfig,
)
from memex_core.context import set_session_id
from memex_core.logging_config import configure_logging
from memex_core.server.audit import router as audit_router
from memex_core.server.auth import auth_middleware, setup_auth
from memex_core.server.rate_limit import setup_rate_limiting
from memex_core.services.audit import AuditService
from memex_core.server.kv import router as kv_router
from memex_core.server.notes import router as notes_router
from memex_core.server.entities import router as entities_router
from memex_core.server.ingestion import router as ingestion_router
from memex_core.server.memories import router as memories_router
from memex_core.server.reflection import router as reflection_router
from memex_core.server.resources import router as resources_router
from memex_core.server.retrieval import router as retrieval_router
from memex_core.server.stats import router as stats_router
from memex_core.server.health import router as health_router
from memex_core.server.summary import router as summary_router
from memex_core.server.vaults import router as vaults_router
from memex_core.scheduler import run_scheduler_with_leader_election
from memex_core.storage.filestore import get_filestore
from memex_core.storage.metastore import get_metastore
from memex_core.memory.models import (
    get_embedding_model,
    get_reranking_model,
    get_ner_model,
    configure_cache_dir,
)

logger = logging.getLogger('memex.core.server')

# Parse CORS config at module level (middleware must be added before app starts).
# The full config is re-parsed in lifespan() so test fixtures can set env vars
# before the server starts.
_cors_config = parse_memex_config().server.cors


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle management for the Memex API."""
    config = parse_memex_config()

    # Configure structured logging via structlog
    log_level = os.getenv('MEMEX_LOG_LEVEL', config.server.logging.level)
    configure_logging(level=log_level, json_output=config.server.logging.json_output)
    setup_rate_limiting(app, config.server.rate_limit)
    setup_auth(app, config.server.auth)

    # Set up OpenTelemetry tracing if enabled
    if config.server.tracing.enabled:
        from memex_core.tracing import setup_tracing

        setup_tracing(config.server.tracing)

    # Refuse to bind to a non-localhost address without authentication
    _is_localhost = config.server.host in ('127.0.0.1', 'localhost', '::1')
    if not _is_localhost and not config.server.auth.enabled:
        if config.server.allow_insecure:
            logger.warning(
                'Server is binding to %s without authentication (--allow-insecure). '
                'This is NOT recommended for production.',
                config.server.host,
            )
        else:
            raise RuntimeError(
                f'Refusing to bind to {config.server.host} without authentication. '
                'Either enable auth (server.auth.enabled=true) or set '
                'server.allow_insecure=true to override this check.'
            )

    metastore = get_metastore(config.server.meta_store)
    filestore = get_filestore(config.server.file_store)

    if not await filestore.check_connection():
        raise RuntimeError(
            f'File store backend ({type(filestore).__name__}) is not reachable. '
            'Check your configuration and ensure the storage service is running.'
        )

    create_schema = os.getenv('MEMEX_SKIP_SCHEMA_CHECK', 'false').lower() != 'true'
    await metastore.connect(create_schema=create_schema)

    configure_cache_dir(config.server.cache_dir)
    embedding_model = await get_embedding_model()
    reranking_model = await get_reranking_model()
    ner_model = await get_ner_model()

    api = MemexAPI(
        embedding_model=embedding_model,
        reranking_model=reranking_model,
        ner_model=ner_model,
        metastore=metastore,
        filestore=filestore,
        config=config,
    )
    await api.initialize()

    try:
        active_vault_name = config.server.default_active_vault
        active_vault_id = await api.resolve_vault_identifier(active_vault_name)
        reader_vault = config.server.default_reader_vault
        logger.info(
            'Memex server started. Active vault: "%s" (id: %s)',
            active_vault_name,
            active_vault_id,
        )
        if reader_vault != active_vault_name:
            logger.info('Default reader vault: %s', reader_vault)

        # Detect if local config overrides the active vault
        local_data = LocalYamlConfigSettingsSource(MemexConfig)()
        local_vault = (local_data.get('server', {}) or {}).get('default_active_vault')
        if local_vault and local_vault != GLOBAL_VAULT_NAME:
            logger.info(
                'Notice: active vault overridden by local config to "%s".',
                local_vault,
            )
    except (MemexError, ValueError) as e:
        logger.warning('Could not resolve active vault info: %s', e)

    app.state.api = api
    app.state.audit_service = AuditService(metastore)

    # Start Scheduler Background Task
    scheduler_task = asyncio.create_task(run_scheduler_with_leader_election(config, api))

    yield

    # Shutdown
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass

    await metastore.close()


app = FastAPI(title='Memex Core API', lifespan=lifespan)

# Configure CORS (must be added before app starts, not in lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_config.origins,
    allow_origin_regex=_cors_config.allow_origin_regex,
    allow_credentials=_cors_config.allow_credentials,
    allow_methods=_cors_config.allow_methods,
    allow_headers=_cors_config.allow_headers,
)

Instrumentator().instrument(app).expose(app, endpoint='/api/v1/metrics')

# Auth middleware: reads app.state.auth_config (set by setup_auth in lifespan).
# Registered at module level so it's part of the middleware stack before app starts.
app.middleware('http')(auth_middleware)


@app.middleware('http')
async def set_request_session_id(request: Request, call_next):
    """
    Middleware to ensure every request has a session ID.
    If 'X-Session-ID' header is present, uses it.
    Otherwise, generates a new one.
    Binds session_id to structlog contextvars for automatic inclusion in log entries.
    """
    session_id = request.headers.get('X-Session-ID')
    sid = set_session_id(session_id)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(session_id=sid)
    response = await call_next(request)
    response.headers['X-Session-ID'] = sid
    return response


# Route modules
app.include_router(ingestion_router)
app.include_router(retrieval_router)
app.include_router(reflection_router)
app.include_router(vaults_router)
app.include_router(notes_router)
app.include_router(stats_router)
app.include_router(entities_router)
app.include_router(memories_router)
app.include_router(resources_router)
app.include_router(health_router)
app.include_router(summary_router)
app.include_router(audit_router)
app.include_router(kv_router)
