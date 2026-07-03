"""Gate: the keyword CTE ranks/filters on the STORED search_tsvector column,
not a per-row to_tsvector(text) recompute.

Functional correctness (keyword search still returns the same docs) is covered by
the document_search integration suite; the migration parity is covered by
test_migrations. This pins the specific perf property the change exists for.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql

from memex_common.schemas import NoteSearchRequest
from memex_core.memory.retrieval.document_search import NoteSearchEngine


def test_keyword_cte_uses_stored_tsvector_not_recompute() -> None:
    engine = NoteSearchEngine(embedder=MagicMock())
    request = NoteSearchRequest(query='mcp servers')
    stmt = engine._keyword_cte('mcp servers', 10, request, {})
    sql = str(stmt.compile(dialect=postgresql.dialect()))

    # Filters + ts_rank_cd use the materialized columns.
    assert 'search_tsvector' in sql
    # The per-row recompute (the cost this change removes) is gone for both tables.
    assert "to_tsvector('english', nodes.text)" not in sql
    assert "to_tsvector('english', chunks.text)" not in sql
