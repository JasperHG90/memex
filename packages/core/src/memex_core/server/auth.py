"""API key authentication middleware for the Memex server."""

import logging
import secrets

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from memex_common.config import AuthConfig

logger = logging.getLogger('memex.core.server')


def _validate_key(api_key: str, auth_config: AuthConfig) -> bool:
    """Check api_key against all configured keys using constant-time comparison.

    Uses ``secrets.compare_digest`` to prevent timing side-channel attacks.
    """
    for valid_key in auth_config.api_keys:
        if secrets.compare_digest(api_key, valid_key.get_secret_value()):
            return True
    return False


def setup_auth(app: FastAPI, auth_config: AuthConfig) -> None:
    """Install the authentication middleware on *app*.

    When ``auth_config.enabled`` is ``False`` this is a no-op — no middleware
    is registered and all requests pass through freely.
    """
    if not auth_config.enabled:
        logger.info('API key authentication is disabled.')
        return

    if not auth_config.api_keys:
        logger.warning(
            'Authentication is enabled but no API keys are configured. '
            'All authenticated requests will be rejected.',
        )

    # Store config on app.state so other components can inspect it.
    app.state.auth_config = auth_config

    @app.middleware('http')
    async def authenticate_request(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in auth_config.exempt_paths:
            return await call_next(request)

        api_key = request.headers.get('X-API-Key')
        if not api_key:
            return JSONResponse(
                status_code=401,
                content={'detail': 'Missing API key. Provide X-API-Key header.'},
            )

        if not _validate_key(api_key, auth_config):
            return JSONResponse(
                status_code=403,
                content={'detail': 'Invalid API key.'},
            )

        return await call_next(request)

    logger.info(
        'API key authentication enabled (%d key(s) configured, %d exempt path(s)).',
        len(auth_config.api_keys),
        len(auth_config.exempt_paths),
    )
