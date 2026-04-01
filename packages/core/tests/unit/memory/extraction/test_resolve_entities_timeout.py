"""Test that entity resolution gracefully handles statement timeouts."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from memex_core.memory.extraction.engine import ExtractionEngine


def _make_dbapi_error_with_query_canceled() -> DBAPIError:
    """Create a DBAPIError wrapping an asyncpg QueryCanceledError."""
    # Create a fake original exception whose class name is QueryCanceledError
    orig = type('QueryCanceledError', (Exception,), {})(
        'canceling statement due to statement timeout'
    )
    err = DBAPIError('statement', {}, orig)
    return err


def _make_dbapi_error_other() -> DBAPIError:
    """Create a DBAPIError that is NOT a statement timeout."""
    orig = type('UniqueViolationError', (Exception,), {})(
        'duplicate key value violates unique constraint'
    )
    err = DBAPIError('statement', {}, orig)
    return err


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
async def test_resolve_entities_returns_empty_on_timeout(
    extraction_engine: ExtractionEngine,
) -> None:
    """When entity resolution hits a statement timeout, return empty set."""
    extraction_engine.entity_resolver.resolve_entities_batch.side_effect = (
        _make_dbapi_error_with_query_canceled()
    )
    session = AsyncMock()
    facts = [_make_processed_fact()]

    result = await extraction_engine._resolve_entities(session, [str(uuid4())], facts)

    assert result == set()
    extraction_engine.entity_resolver.link_units_to_entities_batch.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_entities_reraises_non_timeout_errors(
    extraction_engine: ExtractionEngine,
) -> None:
    """Non-timeout DBAPIErrors should propagate."""
    extraction_engine.entity_resolver.resolve_entities_batch.side_effect = _make_dbapi_error_other()
    session = AsyncMock()
    facts = [_make_processed_fact()]

    with pytest.raises(DBAPIError):
        await extraction_engine._resolve_entities(session, [str(uuid4())], facts)


@pytest.mark.asyncio
async def test_link_entities_returns_empty_on_timeout(
    extraction_engine: ExtractionEngine,
) -> None:
    """When entity linking hits a statement timeout, return empty set."""
    extraction_engine.entity_resolver.resolve_entities_batch.return_value = [str(uuid4())]
    extraction_engine.entity_resolver.link_units_to_entities_batch.side_effect = (
        _make_dbapi_error_with_query_canceled()
    )
    session = AsyncMock()
    facts = [_make_processed_fact()]

    result = await extraction_engine._resolve_entities(session, [str(uuid4())], facts)

    assert result == set()
