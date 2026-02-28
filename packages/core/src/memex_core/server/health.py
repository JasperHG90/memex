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
    """Readiness probe — returns 200 when DB is reachable, 503 otherwise."""
    api = request.app.state.api
    try:
        async with api.metastore.session() as session:
            await session.execute(text('SELECT 1'))
        return JSONResponse(content={'status': 'ok'}, status_code=200)
    except Exception as e:
        logger.warning('Readiness check failed: database unreachable: %s', e, exc_info=True)
        return JSONResponse(content={'status': 'unavailable'}, status_code=503)
