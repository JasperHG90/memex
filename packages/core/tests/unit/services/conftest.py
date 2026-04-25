"""Shared mock helpers for ``vault_summary`` service tests.

Used by ``test_vault_summary_service.py`` and ``test_vault_summary_regression.py``.
Pytest auto-discovers this file and adds its directory to ``sys.path``, so
sibling test modules can ``from conftest import _mock_inventory_session``.

Adding a new SQL query in ``_compute_inventory`` requires updating only
``_mock_inventory_session`` here, not every individual test.
"""

from unittest.mock import AsyncMock, MagicMock


def _scalar_result(value):
    r = MagicMock()
    r.scalar = MagicMock(return_value=value)
    return r


def _one_or_none_result(value):
    r = MagicMock()
    r.one_or_none = MagicMock(return_value=value)
    return r


def _all_result(rows):
    r = MagicMock()
    r.all = MagicMock(return_value=rows)
    return r


def _mock_inventory_session(
    *,
    total_notes=0,
    total_entities=0,
    date_range=None,
    doc_metadata=None,
    recent_7d=0,
    recent_30d=0,
    last_activity=None,
):
    """Mock the 7 SQL queries issued by ``_compute_inventory``, keyed by purpose."""
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _scalar_result(total_notes),
            _scalar_result(total_entities),
            _one_or_none_result(date_range),
            _all_result(doc_metadata or []),
            _scalar_result(recent_7d),
            _scalar_result(recent_30d),
            _scalar_result(last_activity),
        ]
    )
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, session
