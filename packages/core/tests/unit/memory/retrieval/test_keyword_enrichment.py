"""Tests for keyword strategy enhancement with enriched metadata."""

from sqlalchemy.sql import Select

from memex_core.memory.retrieval.strategies import KeywordStrategy


def test_keyword_tsvector_includes_enriched_tags():
    """Verify the SQL statement uses concat_ws and jsonb_extract_path_text for enrichment."""
    strategy = KeywordStrategy()
    stmt = strategy.get_statement('compliance', None, limit=10)

    assert isinstance(stmt, Select)
    sql = str(stmt.compile())
    # The enriched metadata fields appear as jsonb_extract_path_text parameters
    assert 'jsonb_extract_path_text' in sql
    assert 'concat_ws' in sql
    # Should have two coalesce calls (one for enriched_tags, one for enriched_keywords)
    assert sql.count('coalesce') >= 2


def test_keyword_strategy_works_without_enriched_metadata():
    """Keyword strategy should work gracefully when no enriched_* keys exist.

    The COALESCE ensures NULL metadata values become empty strings,
    so the tsvector is still valid.
    """
    strategy = KeywordStrategy()
    stmt = strategy.get_statement('deployment', None, limit=10)

    assert isinstance(stmt, Select)
    sql = str(stmt.compile())
    # COALESCE should be present to handle missing metadata
    assert 'coalesce' in sql.lower()
