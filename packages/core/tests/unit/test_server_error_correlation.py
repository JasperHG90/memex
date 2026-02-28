"""Tests for correlation IDs in error responses."""

from unittest.mock import patch

from memex_common.exceptions import (
    MemexError,
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
