"""F8 cursor codec — TC-21-3 round-trip stability + decode robustness.

Pure-unit tests for the opaque cursor used by ``LintService.get_findings``.
The DB-bound filter composition + read-only assertion live in
``tests/integration/services/test_int_f8_lint_query.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from memex_core.services.lint import _decode_cursor, _encode_cursor


def test_cursor_roundtrip_preserves_timestamp_and_id() -> None:
    ts = datetime(2026, 5, 1, 12, 34, 56, 789012, tzinfo=timezone.utc)
    fid = uuid4()
    cursor = _encode_cursor(ts, fid)
    decoded_ts, decoded_id = _decode_cursor(cursor)  # type: ignore[misc]
    assert decoded_ts == ts
    assert decoded_id == fid


def test_cursor_is_url_safe_base64() -> None:
    """Cursors must be safe to drop into URLs without escaping."""
    ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    fid = uuid4()
    cursor = _encode_cursor(ts, fid)
    assert all(c.isalnum() or c in '-_=' for c in cursor)


@pytest.mark.parametrize(
    'bad',
    [
        '',
        'not-base64-!!!',
        'YWJj',  # decodes to "abc" — not JSON
        # valid base64, valid JSON, missing keys:
        'eyJ0cyI6IDEyM30=',  # {"ts": 123}
        'eyJpZCI6ICJub3QtdXVpZCJ9',  # {"id": "not-uuid"}
    ],
)
def test_decode_cursor_returns_none_on_malformed_input(bad: str) -> None:
    """Malformed cursors degrade gracefully — page 1 instead of 500."""
    assert _decode_cursor(bad) is None


def test_cursor_total_order_under_same_timestamp() -> None:
    """Two findings at the exact same created_at sort by id — both encode distinctly."""
    ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    fid_a, fid_b = uuid4(), uuid4()
    assert _encode_cursor(ts, fid_a) != _encode_cursor(ts, fid_b)
