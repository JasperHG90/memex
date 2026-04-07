"""Tests for structured logging configuration."""

import json
import logging

import structlog

from memex_core.logging_config import configure_logging


class TestConfigureLogging:
    """Tests for the configure_logging function."""

    def setup_method(self):
        """Reset logging state before each test."""
        root = logging.getLogger('memex')
        self._original_level = root.level
        self._original_handlers = list(root.handlers)
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()

    def teardown_method(self):
        """Restore logging state after each test."""
        root = logging.getLogger('memex')
        root.handlers.clear()
        for h in self._original_handlers:
            root.addHandler(h)
        root.setLevel(self._original_level)
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()

    def test_configure_sets_log_level(self):
        """configure_logging sets the level on the memex root logger."""
        configure_logging(level='DEBUG')
        root = logging.getLogger('memex')
        assert root.level == logging.DEBUG

    def test_configure_default_level(self):
        """Default level is WARNING."""
        configure_logging()
        root = logging.getLogger('memex')
        assert root.level == logging.WARNING

    def test_configure_adds_handler(self):
        """configure_logging adds exactly one StreamHandler."""
        configure_logging()
        root = logging.getLogger('memex')
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)

    def test_configure_clears_existing_handlers(self):
        """configure_logging replaces existing handlers."""
        root = logging.getLogger('memex')
        root.addHandler(logging.StreamHandler())
        root.addHandler(logging.StreamHandler())
        assert len(root.handlers) == 2

        configure_logging()
        assert len(root.handlers) == 1

    def test_json_output_produces_json(self, capfd):
        """JSON mode renders log entries as JSON."""
        configure_logging(level='INFO', json_output=True)
        test_logger = logging.getLogger('memex.test.json')
        test_logger.info('test message')

        captured = capfd.readouterr()
        data = json.loads(captured.err.strip())
        assert data['event'] == 'test message'
        assert data['level'] == 'info'
        assert 'timestamp' in data

    def test_console_output_not_json(self, capfd):
        """Console mode does not produce JSON."""
        configure_logging(level='INFO', json_output=False)
        test_logger = logging.getLogger('memex.test.console')
        test_logger.info('console message')

        captured = capfd.readouterr()
        output = captured.err.strip()
        # Console renderer produces human-readable output, not JSON
        assert 'console message' in output
        try:
            json.loads(output)
            raise AssertionError('Console output should not be valid JSON')
        except json.JSONDecodeError:
            pass  # Expected

    def test_child_logger_inherits_formatting(self, capfd):
        """Child loggers of 'memex' use the structlog formatter."""
        configure_logging(level='DEBUG', json_output=True)
        child = logging.getLogger('memex.core.api')
        child.debug('child log entry')

        captured = capfd.readouterr()
        data = json.loads(captured.err.strip())
        assert data['event'] == 'child log entry'
        assert data['logger'] == 'memex.core.api'

    def test_contextvars_included_in_output(self, capfd):
        """Bound contextvars appear in structured log output."""
        configure_logging(level='INFO', json_output=True)
        structlog.contextvars.bind_contextvars(session_id='test-session-42')

        test_logger = logging.getLogger('memex.test.ctx')
        test_logger.info('with context')

        captured = capfd.readouterr()
        data = json.loads(captured.err.strip())
        assert data['session_id'] == 'test-session-42'

    def test_case_insensitive_level(self):
        """Level string is case-insensitive."""
        configure_logging(level='debug')
        root = logging.getLogger('memex')
        assert root.level == logging.DEBUG

    def test_invalid_level_defaults_to_warning(self):
        """An invalid level string falls back to WARNING."""
        configure_logging(level='BOGUS')
        root = logging.getLogger('memex')
        assert root.level == logging.WARNING
