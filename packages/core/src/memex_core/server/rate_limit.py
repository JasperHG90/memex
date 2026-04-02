"""Rate limiting configuration using slowapi."""

import logging

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from memex_common.config import RateLimitConfig

logger = logging.getLogger('memex.core.server')

# Paths exempt from rate limiting
EXEMPT_PATHS = frozenset(
    {
        '/api/v1/health',
        '/api/v1/ready',
        '/api/v1/metrics',
    }
)


def _key_func(request: Request) -> str:
    """Extract client identifier for rate limiting."""
    return get_remote_address(request)


def setup_rate_limiting(app: FastAPI, config: RateLimitConfig) -> None:
    """Configure rate limiting on the FastAPI app.

    When config.enabled is False, this is a no-op.
    """
    if not config.enabled:
        logger.info('Rate limiting is disabled.')
        if hasattr(app.state, 'limiter'):
            del app.state.limiter
        return

    limiter = Limiter(
        key_func=_key_func,
        default_limits=[config.default],
        headers_enabled=True,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    logger.info(
        'Rate limiting enabled: ingestion=%s, search=%s, batch=%s, default=%s',
        config.ingestion,
        config.search,
        config.batch,
        config.default,
    )
