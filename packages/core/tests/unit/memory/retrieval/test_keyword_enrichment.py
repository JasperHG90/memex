"""Tests for keyword strategy with stored search_tsvector column."""

from sqlalchemy.sql import Select

from memex_core.memory.retrieval.strategies import KeywordStrategy


def test_keyword_strategy_uses_stored_tsvector():
    """Verify the SQL statement references the search_tsvector column, not on-the-fly computation."""
    strategy = KeywordStrategy()
    stmt = strategy.get_statement('compliance', None, limit=10)

    assert isinstance(stmt, Select)
    sql = str(stmt.compile())
    # Should reference the stored column
    assert 'search_tsvector' in sql, f'Expected search_tsvector column reference in SQL, got: {sql}'
    # Should NOT do on-the-fly computation
    assert 'concat_ws' not in sql, 'Should not use concat_ws with stored tsvector'
    assert 'jsonb_extract_path_text' not in sql, (
        'Should not use jsonb_extract_path_text with stored tsvector'
    )


def test_keyword_strategy_uses_ts_rank_cd():
    """KeywordStrategy should still use ts_rank_cd for scoring."""
    strategy = KeywordStrategy()
    stmt = strategy.get_statement('deployment', None, limit=10)

    assert isinstance(stmt, Select)
    sql = str(stmt.compile())
    assert 'ts_rank_cd' in sql, f'Expected ts_rank_cd in SQL, got: {sql}'


def test_keyword_strategy_returns_score_column():
    """KeywordStrategy should return an id and score column."""
    strategy = KeywordStrategy()
    stmt = strategy.get_statement('test query', None, limit=10)

    assert isinstance(stmt, Select)
    cols = stmt.selected_columns
    assert 'score' in cols, 'Expected score column in selected columns'
