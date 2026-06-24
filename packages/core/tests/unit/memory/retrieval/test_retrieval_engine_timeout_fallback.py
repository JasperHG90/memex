"""Proof that memory-search (RetrievalEngine) degrades to cheap signals on
statement_timeout — the symmetric fix to the note-search fallback.

The inner RRF call is mocked to raise a statement-timeout DBAPIError; we assert the
wrapper rolls back the aborted transaction and retries with only the cheap signals
(semantic + temporal), returning results instead of propagating QueryCanceledError.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import DBAPIError

from memex_core.memory.retrieval.engine import RetrievalEngine


class _TimeoutOrig(Exception):
    """asyncpg adapter Error stand-in carrying SQLSTATE 57014 (query_canceled)."""

    sqlstate = '57014'


def _timeout_error() -> DBAPIError:
    return DBAPIError('stmt', {}, _TimeoutOrig('canceling statement due to statement timeout'))


@pytest.mark.asyncio
async def test_rrf_degrades_to_cheap_signals_on_timeout() -> None:
    engine = RetrievalEngine(embedder=MagicMock(), reranker=None)
    session = AsyncMock()
    fallback_items = [MagicMock()]

    with patch.object(engine, '_perform_rrf_retrieval_inner', new_callable=AsyncMock) as inner:
        inner.side_effect = [_timeout_error(), fallback_items]
        out = await engine._perform_rrf_retrieval(session, 'q', [0.0] * 4, 10, {})

    assert out == fallback_items, 'returns fallback results instead of raising'
    session.rollback.assert_awaited()
    # The retry (last call) used only the cheap signals (6th positional arg = strategies).
    args, _ = inner.call_args
    assert args[5] == ['semantic', 'temporal']


@pytest.mark.asyncio
async def test_rrf_returns_empty_when_fallback_also_times_out() -> None:
    engine = RetrievalEngine(embedder=MagicMock(), reranker=None)
    session = AsyncMock()

    with patch.object(engine, '_perform_rrf_retrieval_inner', new_callable=AsyncMock) as inner:
        inner.side_effect = [_timeout_error(), _timeout_error()]
        out = await engine._perform_rrf_retrieval(session, 'q', [0.0] * 4, 10, {})

    assert out == []
    assert session.rollback.await_count == 2


@pytest.mark.asyncio
async def test_rrf_non_timeout_error_propagates() -> None:
    engine = RetrievalEngine(embedder=MagicMock(), reranker=None)
    session = AsyncMock()

    with patch.object(engine, '_perform_rrf_retrieval_inner', new_callable=AsyncMock) as inner:
        inner.side_effect = DBAPIError('stmt', {}, ValueError('not a timeout'))
        with pytest.raises(DBAPIError):
            await engine._perform_rrf_retrieval(session, 'q', [0.0] * 4, 10, {})
