"""Compile-level gates for the note-graph perf fixes (no DB, plan-independent).

Plan A: the 2nd-order co-occurrence neighbour set is capped to ``max_neighbors``
(top-N by link_strength) BEFORE the fan-out join, bounding the hub-entity blow-up
that drives the note-search statement_timeout.

Plan B: fuzzy seed-entity matching uses the pg_trgm ``%`` operator (sargable via
the gin_trgm_ops indexes) instead of ``similarity(...) > const`` (non-sargable —
it OR-defeated the trigram index into a seqscan of all entities).

Both are asserted against the COMPILED SQL so they hold regardless of the live
plan. The ``%`` operator renders paramstyle-escaped as ``%%``.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from memex_core.memory.retrieval.strategies import (
    EntityCooccurrenceNoteGraphStrategy,
    build_seed_entity_cte,
)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_seed_cte_fallback_uses_trgm_operator_not_similarity_function() -> None:
    """No-NER fallback path: ``lower(col) % term`` (sargable), not ``similarity() > c``."""
    sql = _sql(
        build_seed_entity_cte(
            query='mcp servers',
            ner_model=None,
            similarity_threshold=0.3,
            include_ilike=True,
            enable_semantic_seeding=False,
        )
    )
    # `%` operator renders escaped as `%%` under the psycopg/asyncpg paramstyle.
    assert ' %% ' in sql, 'expected pg_trgm % operator in seed predicate'
    assert 'similarity(' not in sql, 'non-sargable similarity() must be gone (defeats trgm index)'


def test_seed_cte_ner_path_uses_trgm_operator_not_similarity_function() -> None:
    """NER path (pre-extracted entities) also uses the ``%`` operator."""
    sql = _sql(
        build_seed_entity_cte(
            query='mcp servers',
            ner_model=None,
            similarity_threshold=0.3,
            include_ilike=True,
            enable_semantic_seeding=False,
            pre_extracted_entities=[{'word': 'mcp'}],
        )
    )
    assert ' %% ' in sql
    assert 'similarity(' not in sql


def test_note_graph_caps_neighbors_by_max_neighbors() -> None:
    """The doc_graph_related_entities CTE is LIMITed to max_neighbors."""
    distinctive_cap = 7
    strat = EntityCooccurrenceNoteGraphStrategy(
        max_neighbors=distinctive_cap, enable_semantic_seeding=False
    )
    compiled = strat.get_statement('mcp servers', None, limit=60).compile(
        dialect=postgresql.dialect()
    )
    sql = str(compiled)
    compact = ' '.join(sql.split())
    # Structural: the neighbour CTE is ordered by link_strength and LIMITed — this
    # exact fragment is unique to the cap (the final union orders by `score DESC`),
    # so it goes red if the .order_by(...).limit(...) cap is removed. Not a
    # coincidental param-value match.
    assert 'doc_graph_related_entities' in compact
    assert 'ORDER BY link_strength DESC LIMIT' in compact
    # And the cap value is the configured one.
    assert distinctive_cap in list(compiled.params.values())


def test_note_graph_default_cap_is_fifty() -> None:
    """Default max_neighbors is 50 (the configured RetrievalConfig default)."""
    strat = EntityCooccurrenceNoteGraphStrategy(enable_semantic_seeding=False)
    assert strat.max_neighbors == 50
