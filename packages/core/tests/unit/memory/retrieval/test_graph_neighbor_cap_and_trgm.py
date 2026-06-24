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

from sqlalchemy import select
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


def test_seed_cap_bounds_pre_extracted_entity_list() -> None:
    """The pre-extracted NER list (what the MU engine feeds in on the full query)
    is deduped and capped, so a document-sized query can't explode the OR/IN into
    thousands of bound parameters (the prod note-search timeout)."""
    from memex_core.memory.retrieval.strategies import build_seed_entity_cte

    names = [{'word': f'seedname{i:04d}'} for i in range(500)]
    cte = build_seed_entity_cte(
        query='x',
        ner_model=None,
        similarity_threshold=0.3,
        include_ilike=False,
        enable_semantic_seeding=False,
        pre_extracted_entities=names,
    )
    # IN-lists are expanding bind params; render_postcompile expands them so the
    # individual values are visible.
    compiled = select(cte.c.id).compile(
        dialect=postgresql.dialect(), compile_kwargs={'render_postcompile': True}
    )
    seed = {v for v in compiled.params.values() if isinstance(v, str) and v.startswith('seedname')}
    assert 'seedname0000' in seed, 'first names are kept'
    assert 'seedname0100' not in seed, 'names beyond the cap are dropped'
    assert len(seed) <= 64, f'distinct seed names must be capped, got {len(seed)}'


def test_seed_query_truncated_before_like_and_windowing() -> None:
    """A document-sized query is truncated before NER/windowing/LIKE, so no bound
    parameter (e.g. the fallback `LIKE '%<query>%'`) carries the whole document."""
    from memex_core.memory.retrieval.strategies import build_seed_entity_cte

    huge = 'mcpword ' * 5000  # ~40k chars
    cte = build_seed_entity_cte(
        query=huge,
        ner_model=None,  # no NER -> fallback LIKE/% on the (truncated) query
        similarity_threshold=0.3,
        include_ilike=True,
        enable_semantic_seeding=False,
    )
    compiled = select(cte.c.id).compile(dialect=postgresql.dialect())
    str_params = [v for v in compiled.params.values() if isinstance(v, str)]
    # LIKE pattern is '%<truncated>%'; nothing should carry the full 40k query.
    assert str_params, 'fallback should bind at least the LIKE/% query param'
    assert max(len(v) for v in str_params) <= 1024 + 4, 'query must be truncated before binding'
