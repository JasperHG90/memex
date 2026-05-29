"""Tests for correlation IDs and log-level handling in error responses."""

from unittest.mock import patch

import pytest

from memex_common.exceptions import (
    AmbiguousResourceError,
    MemexError,
    ObservationReadOnlyError,
    ResourceNotFoundError,
    VaultNotFoundError,
)
from memex_core.server.common import _handle_error


class TestHandleErrorCorrelationId:
    """Verify _handle_error includes correlation_id in 500 responses."""

    def test_500_includes_correlation_id(self):
        with patch('memex_core.server.common.get_session_id', return_value='test-session-123'):
            exc = _handle_error(RuntimeError('unexpected'), 'test context')
        assert exc.status_code == 500
        assert exc.detail['error'] == 'Internal server error'
        assert exc.detail['correlation_id'] == 'test-session-123'

    def test_500_uses_current_session_id(self):
        with patch('memex_core.server.common.get_session_id', return_value='abc-456'):
            exc = _handle_error(ValueError('bad value'), 'test context')
        assert exc.detail['correlation_id'] == 'abc-456'

    def test_404_vault_not_found_unchanged(self):
        exc = _handle_error(VaultNotFoundError('vault gone'), 'test context')
        assert exc.status_code == 404
        assert exc.detail == 'vault gone'

    def test_404_resource_not_found_unchanged(self):
        exc = _handle_error(ResourceNotFoundError('not found'), 'test context')
        assert exc.status_code == 404
        assert exc.detail == 'not found'

    def test_400_memex_error_unchanged(self):
        exc = _handle_error(MemexError('bad request'), 'test context')
        assert exc.status_code == 400
        assert exc.detail == 'bad request'


class TestHandleErrorLogLevels:
    """Verify _handle_error demotes client-not-found errors below ERROR.

    Background: hermes-plugin polls a not-yet-materialised note ID at 10 Hz
    during ingest. Logging every 404 at ERROR with a full traceback flooded
    Nomad logs (~231 ERROR records per 10k lines) and obscured real
    incidents. 404/ambiguous-resource errors are client-side signals and
    belong at INFO, no traceback.

    Tests patch the module-level logger directly rather than rely on caplog
    so they're independent of any global logging configuration applied by
    earlier tests in the suite.
    """

    @pytest.mark.parametrize(
        'exc',
        [
            ResourceNotFoundError('note xyz not found'),
            VaultNotFoundError('vault gone'),
            AmbiguousResourceError('which one?'),
        ],
        ids=['ResourceNotFoundError', 'VaultNotFoundError', 'AmbiguousResourceError'],
    )
    def test_client_not_found_logs_at_info_without_traceback(self, exc: Exception) -> None:
        with patch('memex_core.server.common.logger') as mock_logger:
            _handle_error(exc, 'test context')

        mock_logger.info.assert_called_once()
        mock_logger.error.assert_not_called()
        # exc_info kwarg must NOT be passed on the demoted path.
        assert 'exc_info' not in mock_logger.info.call_args.kwargs

    def test_unknown_exception_still_logs_at_error_with_traceback(self) -> None:
        exc = RuntimeError('boom')
        with patch('memex_core.server.common.logger') as mock_logger:
            with patch('memex_core.server.common.get_session_id', return_value='sid'):
                _handle_error(exc, 'test context')

        mock_logger.error.assert_called_once()
        mock_logger.info.assert_not_called()
        # Catch-all 500 path keeps traceback visibility via explicit exc_info=e.
        assert mock_logger.error.call_args.kwargs.get('exc_info') is exc

    def test_memex_error_subclass_not_in_demote_list_still_logs_error(self) -> None:
        """Generic MemexError (not in the demoted triple) keeps ERROR + traceback.

        Intentional scope: only the three high-volume not-found / ambiguous
        types are demoted. Other MemexError subclasses (AppendIdConflictError,
        FeatureDisabledError, etc.) are lower-volume and may signal real
        problems — keep them loud until proven otherwise.
        """
        exc = MemexError('generic memex error')
        with patch('memex_core.server.common.logger') as mock_logger:
            _handle_error(exc, 'test context')

        mock_logger.error.assert_called_once()
        mock_logger.info.assert_not_called()
        assert mock_logger.error.call_args.kwargs.get('exc_info') is exc

    def test_observation_read_only_excluded_from_demote_even_if_hybrid(self) -> None:
        """ObservationReadOnlyError stays at ERROR even if a future refactor
        makes it inherit from one of the demoted types.

        Defensive: ObservationReadOnlyError carries a structured 400-detail
        shape (source_memory_units). A silent demote to INFO would lose
        traceback visibility for a real "agent misused the API" signal.
        Hybrid class below simulates the future-refactor case Hermes flagged.
        """

        class HybridReadOnlyMissing(ResourceNotFoundError, ObservationReadOnlyError):
            def to_http_detail(self) -> dict:  # type: ignore[override]
                return {'error': 'observation_read_only', 'source_memory_units': []}

        exc = HybridReadOnlyMissing('observation x is read-only')
        with patch('memex_core.server.common.logger') as mock_logger:
            _handle_error(exc, 'test context')

        # Demotion exclusion fires: ERROR with traceback, not INFO.
        mock_logger.error.assert_called_once()
        mock_logger.info.assert_not_called()
        assert mock_logger.error.call_args.kwargs.get('exc_info') is exc
