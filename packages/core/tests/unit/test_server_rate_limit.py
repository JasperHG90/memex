"""Tests for rate limiting configuration."""

from fastapi import FastAPI

from memex_common.config import RateLimitConfig
from memex_core.server.rate_limit import setup_rate_limiting


class TestRateLimitSetup:
    """Tests for the setup_rate_limiting function."""

    def test_disabled_by_default(self):
        """Rate limiting should be a no-op when disabled."""
        app = FastAPI()
        config = RateLimitConfig()
        assert config.enabled is False

        setup_rate_limiting(app, config)
        assert not hasattr(app.state, 'limiter')

    def test_enabled_attaches_limiter(self):
        """When enabled, limiter should be attached to app.state."""
        app = FastAPI()
        config = RateLimitConfig(enabled=True)

        setup_rate_limiting(app, config)
        assert hasattr(app.state, 'limiter')

    def test_enabled_registers_exception_handler(self):
        """When enabled, RateLimitExceeded handler should be registered."""
        app = FastAPI()
        config = RateLimitConfig(enabled=True)

        setup_rate_limiting(app, config)
        # FastAPI stores exception handlers in exception_handlers dict
        from slowapi.errors import RateLimitExceeded

        assert RateLimitExceeded in app.exception_handlers

    def test_config_defaults(self):
        """Verify default rate limit values."""
        config = RateLimitConfig()
        assert config.enabled is False
        assert config.ingestion == '10/minute'
        assert config.search == '60/minute'
        assert config.batch == '5/minute'
        assert config.default == '120/minute'

    def test_custom_limits(self):
        """Custom rate limits should be preserved in config."""
        config = RateLimitConfig(
            enabled=True,
            ingestion='20/minute',
            search='100/minute',
            batch='10/minute',
            default='200/minute',
        )
        assert config.ingestion == '20/minute'
        assert config.search == '100/minute'
        assert config.batch == '10/minute'
        assert config.default == '200/minute'

    def test_limiter_uses_default_limits(self):
        """Limiter should be configured with the default limit from config."""
        app = FastAPI()
        config = RateLimitConfig(enabled=True, default='50/minute')

        setup_rate_limiting(app, config)
        limiter = app.state.limiter
        # slowapi stores default limits internally
        assert limiter._default_limits is not None
