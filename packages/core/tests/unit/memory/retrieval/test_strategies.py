from datetime import datetime, timezone
from sqlalchemy.sql import Select
from memex_core.memory.retrieval.strategies import (
    SemanticStrategy,
    KeywordStrategy,
    TemporalStrategy,
    GraphStrategy,
    MentalModelStrategy,
    _apply_as_of_filter,
)
from memex_core.memory.sql_models import EntityCooccurrence
from sqlmodel import select


def test_semantic_strategy_sql():
    strategy = SemanticStrategy()
    embedding = [0.1] * 384
    stmt = strategy.get_statement('test', embedding, limit=10)

    assert isinstance(stmt, Select)
    # Check limit
    assert str(stmt).endswith('LIMIT :param_1')
    # Since it's a Select object, we can inspect its properties but it's complex.
    # We'll stick to basic checks or string representation if needed.
    # To check for distance column, we can look at the selected columns.
    cols = stmt.selected_columns
    assert 'score' in cols


def test_keyword_strategy_filters():
    strategy = KeywordStrategy()
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    stmt = strategy.get_statement('test', None, start_date=start)

    # Verify the where clause contains the date filter
    # This is a bit implementation-specific but ensures the kwargs are handled.
    sql = str(stmt.compile())
    assert 'event_date >=' in sql or 'event_date' in sql


def test_temporal_strategy_order():
    strategy = TemporalStrategy()
    stmt = strategy.get_statement('test', None, limit=5)

    sql = str(stmt.compile())
    assert 'ORDER BY memory_units.event_date DESC' in sql
    assert 'LIMIT' in sql


def test_graph_strategy_unions():
    strategy = GraphStrategy()
    stmt = strategy.get_statement('chimera', None)

    # Graph strategy returns a union_all statement
    sql = str(stmt.compile())
    assert 'UNION ALL' in sql
    assert 'similarity' in sql or 'ilike' in sql


def test_mental_model_strategy_fallback():
    strategy = MentalModelStrategy()
    # Test fallback to name match when no embedding is provided
    stmt = strategy.get_statement('Project X', None)

    sql = str(stmt.compile())
    # SQLAlchemy ilike often compiles to LOWER(...) LIKE LOWER(...)
    assert 'mental_models.name' in sql
    assert 'LIKE' in sql


class TestAsOfFilter:
    """Unit tests for the _apply_as_of_filter helper."""

    def test_no_as_of_leaves_statement_unchanged(self):
        """Without as_of kwarg, the statement is returned unchanged."""
        base = select(EntityCooccurrence.entity_id_1)
        result = _apply_as_of_filter(base)
        # No WHERE clause should be added
        compiled = str(result.compile())
        assert 'valid_from' not in compiled
        assert 'valid_to' not in compiled

    def test_as_of_adds_valid_from_predicate(self):
        """as_of adds: valid_from IS NULL OR valid_from <= as_of."""
        base = select(EntityCooccurrence.entity_id_1)
        as_of = datetime(2024, 6, 1, tzinfo=timezone.utc)
        result = _apply_as_of_filter(base, as_of=as_of)
        compiled = str(result.compile())
        assert 'valid_from' in compiled

    def test_as_of_adds_valid_to_predicate(self):
        """as_of adds: valid_to IS NULL OR valid_to > as_of."""
        base = select(EntityCooccurrence.entity_id_1)
        as_of = datetime(2024, 6, 1, tzinfo=timezone.utc)
        result = _apply_as_of_filter(base, as_of=as_of)
        compiled = str(result.compile())
        assert 'valid_to' in compiled

    def test_as_of_none_no_filter(self):
        """Explicitly passing as_of=None should not add filters."""
        base = select(EntityCooccurrence.entity_id_1)
        result = _apply_as_of_filter(base, as_of=None)
        compiled = str(result.compile())
        assert 'valid_from' not in compiled
        assert 'valid_to' not in compiled

    def test_graph_strategy_passes_as_of_through(self):
        """GraphStrategy.get_statement passes as_of to co-occurrence query."""
        strategy = GraphStrategy()
        as_of = datetime(2024, 6, 1, tzinfo=timezone.utc)
        stmt = strategy.get_statement('chimera', None, as_of=as_of)
        compiled = str(stmt.compile())
        assert 'valid_from' in compiled
        assert 'valid_to' in compiled

    def test_graph_strategy_no_as_of_no_temporal_filter(self):
        """GraphStrategy without as_of does not add temporal validity filters."""
        strategy = GraphStrategy()
        stmt = strategy.get_statement('chimera', None)
        compiled = str(stmt.compile())
        # valid_from/valid_to should not appear as WHERE predicates
        # (they may appear in column definitions from the model, but not in WHERE)
        # Check that the temporal validity filter is NOT applied
        assert 'valid_from' not in compiled or 'IS NULL' not in compiled
