"""Entity resolution/linking run inside the caller's single ingest transaction
(no savepoint), so a statement_timeout there must PROPAGATE — the caller's
AsyncTransaction owns the rollback of the whole note. These guard against
re-introducing an in-place skip/rollback that would discard the note's
already-persisted facts (an earlier attempt did exactly that).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from memex_core.memory.extraction.engine import ExtractionEngine


def _make_dbapi_error_with_query_canceled() -> DBAPIError:
    """A DBAPIError shaped like a real statement_timeout (SQLSTATE 57014)."""
    orig = type('Error', (Exception,), {'sqlstate': '57014'})(
        'canceling statement due to statement timeout'
    )
    return DBAPIError('statement', {}, orig)


def _make_dbapi_error_other() -> DBAPIError:
    """A DBAPIError that is NOT a statement timeout (unique_violation 23505)."""
    orig = type('Error', (Exception,), {'sqlstate': '23505'})(
        'duplicate key value violates unique constraint'
    )
    return DBAPIError('statement', {}, orig)


def _make_processed_fact() -> MagicMock:
    fact = MagicMock()
    fact.entities = [MagicMock(text='TestEntity', entity_type='Concept')]
    fact.occurred_start = None
    fact.mentioned_at = datetime.now(timezone.utc)
    fact.who = None
    fact.where = None
    return fact


@pytest.fixture
def extraction_engine() -> ExtractionEngine:
    engine = ExtractionEngine.__new__(ExtractionEngine)
    engine.entity_resolver = MagicMock()
    engine.entity_resolver.resolve_entities_batch = AsyncMock()
    engine.entity_resolver.link_units_to_entities_batch = AsyncMock()
    return engine


@pytest.mark.asyncio
async def test_resolution_timeout_propagates_without_touching_shared_txn(
    extraction_engine: ExtractionEngine,
) -> None:
    """A timeout during resolution propagates; we do NOT roll back the shared txn
    (that would discard the note's already-persisted facts) — the caller does."""
    extraction_engine.entity_resolver.resolve_entities_batch.side_effect = (
        _make_dbapi_error_with_query_canceled()
    )
    session = AsyncMock()
    facts = [_make_processed_fact()]

    with pytest.raises(DBAPIError):
        await extraction_engine._resolve_entities(session, [str(uuid4())], facts)

    session.rollback.assert_not_awaited()
    extraction_engine.entity_resolver.link_units_to_entities_batch.assert_not_called()


@pytest.mark.asyncio
async def test_non_timeout_db_error_propagates(
    extraction_engine: ExtractionEngine,
) -> None:
    """Non-timeout DBAPIErrors propagate too (no special-casing)."""
    extraction_engine.entity_resolver.resolve_entities_batch.side_effect = _make_dbapi_error_other()
    session = AsyncMock()
    facts = [_make_processed_fact()]

    with pytest.raises(DBAPIError):
        await extraction_engine._resolve_entities(session, [str(uuid4())], facts)


@pytest.mark.asyncio
async def test_linking_timeout_propagates_without_touching_shared_txn(
    extraction_engine: ExtractionEngine,
) -> None:
    extraction_engine.entity_resolver.resolve_entities_batch.return_value = [str(uuid4())]
    extraction_engine.entity_resolver.link_units_to_entities_batch.side_effect = (
        _make_dbapi_error_with_query_canceled()
    )
    session = AsyncMock()
    facts = [_make_processed_fact()]

    with pytest.raises(DBAPIError):
        await extraction_engine._resolve_entities(session, [str(uuid4())], facts)

    session.rollback.assert_not_awaited()
