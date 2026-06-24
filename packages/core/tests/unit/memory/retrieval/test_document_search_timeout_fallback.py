"""Proof that hybrid note-search degrades to cheap signals on statement_timeout
instead of failing the whole request (the graceful-degradation safety net).

These pin the control flow deterministically (no DB): the inner per-query call is
mocked to raise a statement-timeout DBAPIError, and we assert the wrapper rolls
back the aborted transaction and retries with only the cheap signals — returning
results rather than propagating QueryCanceledError. The no-regression guarantee
(normal queries are unaffected) is covered by the existing document_search
integration suite, which exercises the non-timeout path end to end.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError

from memex_common.schemas import NoteSearchRequest
from memex_core.memory.retrieval.document_search import NoteSearchEngine


class _TimeoutOrig(Exception):
    """Stand-in for the asyncpg adapter Error: carries SQLSTATE 57014 (query_canceled)."""

    sqlstate = '57014'


def _timeout_error() -> DBAPIError:
    return DBAPIError('stmt', {}, _TimeoutOrig('canceling statement due to statement timeout'))


@pytest.mark.asyncio
async def test_fallback_degrades_to_cheap_signals_on_timeout() -> None:
    engine = NoteSearchEngine(embedder=MagicMock())
    request = NoteSearchRequest(query='x')  # defaults to all four strategies
    session = AsyncMock()
    fallback_results = [MagicMock()]

    dropped: set[str] = set()
    with patch.object(engine, '_search_single_query', new_callable=AsyncMock) as inner:
        inner.side_effect = [_timeout_error(), fallback_results]
        out = await engine._search_single_query_with_fallback(
            session, 'x', [0.0] * 4, 10, request, dropped
        )

    assert out == fallback_results, 'returns the fallback results instead of raising'
    session.rollback.assert_awaited()  # aborted txn cleared before retry
    # The retry dropped graph + keyword, keeping only the cheap signals.
    _, kwargs = inner.call_args
    assert kwargs.get('active_override') == {'semantic', 'temporal'}
    # The dropped strategies are recorded for the degraded response signal.
    assert dropped == {'graph', 'keyword'}


@pytest.mark.asyncio
async def test_returns_empty_when_fallback_also_times_out() -> None:
    engine = NoteSearchEngine(embedder=MagicMock())
    request = NoteSearchRequest(query='x')
    session = AsyncMock()

    dropped: set[str] = set()
    with patch.object(engine, '_search_single_query', new_callable=AsyncMock) as inner:
        inner.side_effect = [_timeout_error(), _timeout_error()]
        out = await engine._search_single_query_with_fallback(
            session, 'x', [0.0] * 4, 10, request, dropped
        )

    assert out == [], 'both timed out -> this query contributes nothing, but no raise'
    assert session.rollback.await_count == 2
    assert dropped == {'graph', 'keyword'}


@pytest.mark.asyncio
async def test_non_timeout_db_error_propagates() -> None:
    """A non-timeout DB error is NOT swallowed — only statement_timeout degrades."""
    engine = NoteSearchEngine(embedder=MagicMock())
    request = NoteSearchRequest(query='x')
    session = AsyncMock()

    with patch.object(engine, '_search_single_query', new_callable=AsyncMock) as inner:
        inner.side_effect = DBAPIError('stmt', {}, ValueError('not a timeout'))
        with pytest.raises(DBAPIError):
            await engine._search_single_query_with_fallback(
                session, 'x', [0.0] * 4, 10, request, set()
            )
