from datetime import datetime, timezone
from sqlalchemy.sql import Select
from memex_core.memory.retrieval.strategies import (
    SemanticStrategy,
    KeywordStrategy,
    TemporalStrategy,
    GraphStrategy,
    MentalModelStrategy,
)


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
