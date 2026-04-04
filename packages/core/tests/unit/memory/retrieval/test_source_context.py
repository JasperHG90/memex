"""Tests for source_context filtering (Feature B: AC-B01, AC-B03)."""

from sqlalchemy.sql import Select

from memex_core.memory.retrieval.models import RetrievalRequest
from memex_core.memory.retrieval.strategies import (
    SemanticStrategy,
    KeywordStrategy,
    TemporalStrategy,
    GraphStrategy,
    MentalModelStrategy,
    apply_context_filter,
)


# ---------------------------------------------------------------------------
# AC-B01: RetrievalRequest has source_context field
# ---------------------------------------------------------------------------


def test_retrieval_request_accepts_source_context():
    """AC-B01: RetrievalRequest can be constructed with source_context='user_notes'."""
    req = RetrievalRequest(query='test query', source_context='user_notes')
    assert req.source_context == 'user_notes'


def test_retrieval_request_source_context_defaults_to_none():
    req = RetrievalRequest(query='test query')
    assert req.source_context is None


# ---------------------------------------------------------------------------
# apply_context_filter unit tests
# ---------------------------------------------------------------------------


def test_apply_context_filter_adds_where_clause():
    """When source_context is set, the filter adds a WHERE clause on MemoryUnit.context."""
    from sqlmodel import select
    from memex_core.memory.sql_models import MemoryUnit

    stmt = select(MemoryUnit.id)
    filtered = apply_context_filter(stmt, source_context='user_notes')
    sql = str(filtered.compile())
    assert 'memory_units' in sql
    assert 'context' in sql


def test_apply_context_filter_noop_when_none():
    """When source_context is not set, the filter is a no-op."""
    from sqlmodel import select
    from memex_core.memory.sql_models import MemoryUnit

    stmt = select(MemoryUnit.id)
    original_sql = str(stmt.compile())
    filtered = apply_context_filter(stmt)
    assert str(filtered.compile()) == original_sql


# ---------------------------------------------------------------------------
# AC-B03: Each strategy applies context filter when source_context is set
# ---------------------------------------------------------------------------


def _compiled_sql(stmt: Select) -> str:
    return str(stmt.compile())


def test_semantic_strategy_applies_context_filter():
    strategy = SemanticStrategy()
    embedding = [0.1] * 384
    stmt = strategy.get_statement('test', embedding, limit=10, source_context='user_notes')
    sql = _compiled_sql(stmt)
    assert 'context' in sql


def test_semantic_strategy_no_filter_without_source_context():
    strategy = SemanticStrategy()
    embedding = [0.1] * 384
    stmt = strategy.get_statement('test', embedding, limit=10)
    sql = _compiled_sql(stmt)
    # 'context' should not appear as a WHERE filter (only in column list)
    # Check that there's no WHERE ... context = ... clause
    assert 'context =' not in sql


def test_keyword_strategy_applies_context_filter():
    strategy = KeywordStrategy()
    stmt = strategy.get_statement('test', None, limit=10, source_context='user_notes')
    sql = _compiled_sql(stmt)
    assert 'context' in sql


def test_temporal_strategy_applies_context_filter():
    strategy = TemporalStrategy()
    stmt = strategy.get_statement('test', None, limit=10, source_context='user_notes')
    sql = _compiled_sql(stmt)
    assert 'context' in sql


def test_graph_strategy_applies_context_filter():
    strategy = GraphStrategy()
    stmt = strategy.get_statement('chimera', None, limit=10, source_context='user_notes')
    sql = _compiled_sql(stmt)
    assert 'context' in sql


def test_mental_model_strategy_does_not_apply_context_filter():
    """MentalModelStrategy queries mental_models table — no context column."""
    strategy = MentalModelStrategy()
    stmt = strategy.get_statement('test', None, limit=10, source_context='user_notes')
    sql = _compiled_sql(stmt)
    # MentalModel table has no context column, so filter should not appear
    assert 'memory_units' not in sql
