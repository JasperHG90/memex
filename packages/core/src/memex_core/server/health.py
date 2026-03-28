"""Health check endpoints (liveness + readiness)."""

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlmodel import text
from starlette.responses import JSONResponse

logger = logging.getLogger('memex.core.server')

router = APIRouter(prefix='/api/v1')


class HealthResponse(BaseModel):
    status: str


@router.get('/health', response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe — always returns 200 if the process is running."""
    return HealthResponse(status='ok')


@router.get('/ready', response_model=HealthResponse)
async def ready(request: Request) -> JSONResponse:
    """Readiness probe — returns 200 when DB and file store are reachable, 503 otherwise."""
    api = request.app.state.api
    checks: dict[str, str] = {}

    try:
        async with api.metastore.session() as session:
            conn = await session.connection()
            await conn.execute(text('SELECT 1'))
        checks['database'] = 'ok'
    except Exception as e:
        logger.warning('Readiness: database unreachable: %s', e, exc_info=True)
        checks['database'] = 'unavailable'

    try:
        fs_ok = await api.filestore.check_connection()
        checks['filestore'] = 'ok' if fs_ok else 'unavailable'
    except Exception as e:
        logger.warning('Readiness: filestore unreachable: %s', e, exc_info=True)
        checks['filestore'] = 'unavailable'

    if api.config.server.tracing.enabled:
        try:
            from memex_core.tracing import check_tracing_health

            checks['tracing'] = 'ok' if check_tracing_health() else 'unavailable'
        except Exception as e:
            logger.warning('Readiness: tracing check failed: %s', e, exc_info=True)
            checks['tracing'] = 'unavailable'

    all_ok = all(v == 'ok' for v in checks.values())
    return JSONResponse(
        content={'status': 'ok' if all_ok else 'unavailable', **checks},
        status_code=200 if all_ok else 503,
    )
