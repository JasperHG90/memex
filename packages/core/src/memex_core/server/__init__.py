import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from prometheus_fastapi_instrumentator import Instrumentator

from memex_core.api import MemexAPI
from memex_core.config import parse_memex_config
from memex_common.config import (
    GLOBAL_VAULT_NAME,
    LocalYamlConfigSettingsSource,
    MemexConfig,
)
from memex_core.context import set_session_id
from memex_core.server.notes import router as notes_router
from memex_core.server.entities import router as entities_router
from memex_core.server.ingestion import router as ingestion_router
from memex_core.server.memories import router as memories_router
from memex_core.server.reflection import router as reflection_router
from memex_core.server.resources import router as resources_router
from memex_core.server.retrieval import router as retrieval_router
from memex_core.server.stats import router as stats_router
from memex_core.server.summary import router as summary_router
from memex_core.server.vaults import router as vaults_router
from memex_core.scheduler import run_scheduler_with_leader_election
from memex_core.storage.filestore import get_filestore
from memex_core.storage.metastore import get_metastore
from memex_core.memory.models import get_embedding_model, get_reranking_model, get_ner_model

logger = logging.getLogger('memex.core.server')


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle management for the Memex API."""
    # Configure application-level logging from env var set by CLI before execvp
    log_level = os.getenv('MEMEX_LOG_LEVEL', 'WARNING')
    memex_logger = logging.getLogger('memex')
    memex_logger.setLevel(getattr(logging, log_level, logging.WARNING))
    if not memex_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s'))
        memex_logger.addHandler(handler)

    config = parse_memex_config()
    metastore = get_metastore(config.server.meta_store)
    filestore = get_filestore(config.server.file_store)

    create_schema = os.getenv('MEMEX_SKIP_SCHEMA_CREATION', 'false').lower() != 'true'
    await metastore.connect(create_schema=create_schema)

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
        active_vault_name = config.server.active_vault
        active_vault_id = await api.resolve_vault_identifier(active_vault_name)
        attached = config.server.attached_vaults
        logger.info(
            'Memex server started. Active vault: "%s" (id: %s)',
            active_vault_name,
            active_vault_id,
        )
        if attached:
            logger.info('Attached vaults: %s', attached)

        # Detect if local config overrides the active vault
        local_data = LocalYamlConfigSettingsSource(MemexConfig)()
        local_vault = (local_data.get('server', {}) or {}).get('active_vault')
        if local_vault and local_vault != GLOBAL_VAULT_NAME:
            logger.info(
                'Notice: active vault overridden by local config to "%s".',
                local_vault,
            )
    except Exception as e:
        logger.warning('Could not resolve active vault info: %s', e)

    app.state.api = api

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
Instrumentator().instrument(app).expose(app, endpoint='/api/v1/metrics')


@app.middleware('http')
async def set_request_session_id(request: Request, call_next):
    """
    Middleware to ensure every request has a session ID.
    If 'X-Session-ID' header is present, uses it.
    Otherwise, generates a new one.
    """
    session_id = request.headers.get('X-Session-ID')
    sid = set_session_id(session_id)
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
app.include_router(summary_router)
