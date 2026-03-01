"""Structured logging configuration for Memex using structlog.

Provides JSON output for production and human-readable console output for development.
Wraps the stdlib logging module so existing getLogger() calls continue to work.
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: str = 'WARNING', json_output: bool = False) -> None:
    """Configure structured logging for Memex.

    Args:
        level: Logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_output: If True, output JSON for log aggregators. Otherwise, use
            a human-readable console renderer.
    """
    # Shared processors applied to both structlog-native and stdlib loggers.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt='iso'),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Configure structlog-native loggers (e.g. structlog.get_logger()).
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    # ProcessorFormatter bridges structlog and stdlib logging.
    # foreign_pre_chain runs on log records originating from stdlib loggers.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger('memex')
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.WARNING))
    root.propagate = False
