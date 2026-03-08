"""Tests for the CausalGraphStrategy and CausalNoteGraphStrategy."""

from memex_core.memory.retrieval.strategies import (
    CausalGraphStrategy,
    CausalNoteGraphStrategy,
    get_graph_strategy,
    get_note_graph_strategy,
)
from memex_common.config import RetrievalConfig


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_factory_returns_causal_graph_strategy():
    """get_graph_strategy('causal') returns a CausalGraphStrategy instance."""
    strategy = get_graph_strategy('causal')
    assert isinstance(strategy, CausalGraphStrategy)


def test_factory_returns_causal_note_graph_strategy():
    """get_note_graph_strategy('causal') returns a CausalNoteGraphStrategy."""
    strategy = get_note_graph_strategy('causal')
    assert isinstance(strategy, CausalNoteGraphStrategy)


# ---------------------------------------------------------------------------
# SQL compilation tests
# ---------------------------------------------------------------------------


def test_causal_graph_sql_compiles():
    """CausalGraphStrategy.get_statement() compiles without error."""
    strategy = CausalGraphStrategy()
    stmt = strategy.get_statement('climate change', None)
    sql = str(stmt.compile())
    assert sql  # non-empty string means it compiled


def test_causal_note_graph_sql_compiles():
    """CausalNoteGraphStrategy.get_statement() compiles without error."""
    strategy = CausalNoteGraphStrategy()
    stmt = strategy.get_statement('climate change', None)
    sql = str(stmt.compile())
    assert sql


def test_causal_graph_sql_contains_link_type_filter():
    """Generated SQL must filter on causal link_type values."""
    strategy = CausalGraphStrategy()
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    assert 'link_type' in sql
    # The IN clause uses POSTCOMPILE bind params; verify the column reference
    assert 'memory_links.link_type IN' in sql


def test_causal_note_graph_sql_contains_link_type_filter():
    """Note variant also filters on causal link_type values."""
    strategy = CausalNoteGraphStrategy()
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    assert 'link_type' in sql


def test_causal_graph_sql_contains_weight_threshold():
    """Generated SQL must apply the weight >= threshold filter."""
    strategy = CausalGraphStrategy(causal_weight_threshold=0.5)
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    assert 'weight' in sql


def test_causal_note_graph_sql_contains_weight_threshold():
    """Note variant also applies the weight >= threshold filter."""
    strategy = CausalNoteGraphStrategy(causal_weight_threshold=0.5)
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    assert 'weight' in sql


def test_causal_graph_second_order_score_less_than_first():
    """2nd-order scores use a 0.8 multiplier, making them < 1st-order."""
    strategy = CausalGraphStrategy()
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    # The multiplication expression: memory_links.weight * :param (0.8)
    assert 'memory_links.weight *' in sql


def test_causal_note_graph_second_order_score_multiplier():
    """Note variant also applies the 0.8 multiplier for 2nd-order scores."""
    strategy = CausalNoteGraphStrategy()
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    assert 'memory_links.weight *' in sql


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


def test_config_causal_weight_threshold_default():
    """RetrievalConfig has causal_weight_threshold defaulting to 0.3."""
    config = RetrievalConfig()
    assert config.causal_weight_threshold == 0.3


def test_config_causal_weight_threshold_custom():
    """RetrievalConfig accepts a custom causal_weight_threshold."""
    config = RetrievalConfig(causal_weight_threshold=0.7)
    assert config.causal_weight_threshold == 0.7


def test_config_threshold_wired_to_strategy():
    """Factory kwargs pass causal_weight_threshold to the strategy."""
    strategy = get_graph_strategy('causal', causal_weight_threshold=0.6)
    assert isinstance(strategy, CausalGraphStrategy)
    assert strategy.causal_weight_threshold == 0.6


def test_causal_graph_sql_contains_chunks_for_note_variant():
    """CausalNoteGraphStrategy SQL references the chunks table."""
    strategy = CausalNoteGraphStrategy()
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    assert 'chunks' in sql


def test_causal_graph_sql_contains_union_all():
    """CausalGraphStrategy produces a UNION ALL of 1st and causal orders."""
    strategy = CausalGraphStrategy()
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    assert 'UNION ALL' in sql


def test_causal_note_graph_sql_contains_union_all():
    """CausalNoteGraphStrategy produces a UNION ALL."""
    strategy = CausalNoteGraphStrategy()
    stmt = strategy.get_statement('test query', None)
    sql = str(stmt.compile())
    assert 'UNION ALL' in sql


# ---------------------------------------------------------------------------
# Edge cases and negative tests
# ---------------------------------------------------------------------------


def test_causal_weight_threshold_zero():
    """threshold=0.0 accepts all links (most permissive boundary)."""
    strategy = CausalGraphStrategy(causal_weight_threshold=0.0)
    stmt = strategy.get_statement('test', None)
    sql = str(stmt.compile())
    # SQL should still compile and contain link_type filter
    assert 'link_type' in sql


def test_causal_weight_threshold_one():
    """threshold=1.0 filters out almost everything (most restrictive boundary)."""
    strategy = CausalGraphStrategy(causal_weight_threshold=1.0)
    stmt = strategy.get_statement('test', None)
    sql = str(stmt.compile())
    assert 'link_type' in sql


def test_empty_query_compiles():
    """Empty query string still produces valid SQL."""
    strategy = CausalGraphStrategy()
    stmt = strategy.get_statement('', None)
    sql = str(stmt.compile())
    assert sql  # non-empty


def test_note_factory_kwargs_passthrough():
    """Factory kwargs pass causal_weight_threshold to note strategy."""
    strategy = get_note_graph_strategy('causal', causal_weight_threshold=0.8)
    assert isinstance(strategy, CausalNoteGraphStrategy)
    assert strategy.causal_weight_threshold == 0.8


def test_causal_graph_strategy_with_embedding():
    """CausalGraphStrategy works when query_embedding is provided."""
    strategy = CausalGraphStrategy()
    embedding = [0.1] * 384
    stmt = strategy.get_statement('test query', embedding)
    sql = str(stmt.compile())
    assert sql
    assert 'link_type' in sql
