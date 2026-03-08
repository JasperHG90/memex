"""Tests for the LinkExpansion graph strategy (T4)."""

from sqlalchemy.sql import Select, CompoundSelect

from memex_core.memory.retrieval.strategies import (
    LinkExpansionGraphStrategy,
    LinkExpansionNoteGraphStrategy,
    _GRAPH_STRATEGY_REGISTRY,
    _NOTE_GRAPH_STRATEGY_REGISTRY,
    get_graph_strategy,
    get_note_graph_strategy,
)
from memex_common.config import RetrievalConfig


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------


def test_factory_returns_link_expansion_graph_strategy():
    strategy = get_graph_strategy('link_expansion')
    assert isinstance(strategy, LinkExpansionGraphStrategy)


def test_factory_returns_link_expansion_note_graph_strategy():
    strategy = get_note_graph_strategy('link_expansion')
    assert isinstance(strategy, LinkExpansionNoteGraphStrategy)


def test_registry_keys_present():
    assert 'link_expansion' in _GRAPH_STRATEGY_REGISTRY
    assert 'link_expansion' in _NOTE_GRAPH_STRATEGY_REGISTRY


# ---------------------------------------------------------------------------
# SQL compilation (no DB required)
# ---------------------------------------------------------------------------


def _compile_sql(strategy: object, query: str = 'Alice') -> str:
    stmt = strategy.get_statement(query, None)  # type: ignore[attr-defined]
    return str(stmt.compile(compile_kwargs={'literal_binds': True}))


def test_sql_compiles_without_error():
    strategy = LinkExpansionGraphStrategy()
    stmt = strategy.get_statement('Alice', None)
    assert isinstance(stmt, (Select, CompoundSelect))


def test_note_sql_compiles_without_error():
    strategy = LinkExpansionNoteGraphStrategy()
    stmt = strategy.get_statement('Alice', None)
    assert isinstance(stmt, (Select, CompoundSelect))


def test_sql_contains_entity_expansion():
    sql = _compile_sql(LinkExpansionGraphStrategy())
    sql_lower = sql.lower()
    assert "type='entity'" in sql_lower or "= 'entity'" in sql_lower
    assert 'tanh' in sql_lower
    assert 'count' in sql_lower


def test_sql_contains_semantic_expansion_bidirectional():
    sql = _compile_sql(LinkExpansionGraphStrategy())
    sql_lower = sql.lower()
    assert "= 'semantic'" in sql_lower or "'semantic'" in sql_lower
    # Bidirectional: both from_unit_id and to_unit_id referenced
    assert 'from_unit_id' in sql_lower
    assert 'to_unit_id' in sql_lower


def test_sql_contains_causal_expansion():
    sql = _compile_sql(LinkExpansionGraphStrategy())
    sql_lower = sql.lower()
    assert 'causes' in sql_lower
    assert 'caused_by' in sql_lower
    assert 'enables' in sql_lower
    assert 'prevents' in sql_lower
    # Weight threshold
    assert 'weight' in sql_lower


def test_sql_contains_union_all():
    sql = _compile_sql(LinkExpansionGraphStrategy())
    assert 'UNION ALL' in sql


def test_sql_additive_scoring_uses_sum():
    sql = _compile_sql(LinkExpansionGraphStrategy())
    sql_lower = sql.lower()
    assert 'sum(' in sql_lower


def test_sql_entity_id_is_not_null_filter():
    sql = _compile_sql(LinkExpansionGraphStrategy())
    sql_lower = sql.lower()
    assert 'entity_id is not null' in sql_lower or 'entity_id != null' in sql_lower


def test_config_threshold_wired():
    """Config default matches strategy default, and custom value passes through."""
    cfg = RetrievalConfig()
    assert cfg.link_expansion_causal_threshold == 0.3

    custom = RetrievalConfig(link_expansion_causal_threshold=0.7)
    strategy = LinkExpansionGraphStrategy(causal_threshold=custom.link_expansion_causal_threshold)
    assert strategy.causal_threshold == 0.7

    sql = _compile_sql(strategy)
    sql_lower = sql.lower()
    assert '0.7' in sql_lower
