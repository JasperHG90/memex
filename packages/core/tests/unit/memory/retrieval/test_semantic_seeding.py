"""Tests for semantic-seeded graph discovery (T3)."""

from uuid import uuid4

from sqlalchemy.sql.expression import CTE

from memex_core.memory.retrieval.strategies import (
    EntityCooccurrenceGraphStrategy,
    EntityCooccurrenceNoteGraphStrategy,
    build_seed_entity_cte,
    build_semantic_seed_cte,
)
from memex_common.config import RetrievalConfig


FAKE_EMBEDDING = [0.1] * 384
FAKE_VAULT_ID = uuid4()


def test_build_semantic_seed_cte_returns_valid_cte():
    """build_semantic_seed_cte() returns a CTE with id and weight columns."""
    cte = build_semantic_seed_cte(
        query_embedding=FAKE_EMBEDDING,
        vault_id=FAKE_VAULT_ID,
        top_k=5,
        weight=0.7,
    )
    assert isinstance(cte, CTE)
    assert 'id' in cte.c
    assert 'weight' in cte.c


def test_build_semantic_seed_cte_status_active_filter():
    """SQL contains status = 'active' filter for HNSW index usage."""
    cte = build_semantic_seed_cte(
        query_embedding=FAKE_EMBEDDING,
        vault_id=FAKE_VAULT_ID,
    )
    sql = str(cte.compile())
    # The status filter is parameterized but references memory_units.status
    assert 'memory_units.status' in sql.lower()


def test_build_seed_entity_cte_with_embedding_contains_union():
    """build_seed_entity_cte() with embedding produces UNION ALL for semantic seeds."""
    cte = build_seed_entity_cte(
        query='Who recommended the national park?',
        ner_model=None,
        query_embedding=FAKE_EMBEDDING,
        vault_id=FAKE_VAULT_ID,
        enable_semantic_seeding=True,
    )
    assert isinstance(cte, CTE)
    sql = str(cte.compile())
    assert 'UNION ALL' in sql


def test_build_seed_entity_cte_without_embedding_no_union():
    """build_seed_entity_cte() without embedding is identical to before (no UNION)."""
    cte = build_seed_entity_cte(
        query='chimera',
        ner_model=None,
        query_embedding=None,
        enable_semantic_seeding=True,
    )
    assert isinstance(cte, CTE)
    sql = str(cte.compile())
    assert 'UNION ALL' not in sql


def test_build_seed_entity_cte_semantic_disabled_no_union():
    """build_seed_entity_cte() with enable_semantic_seeding=False produces no semantic seeds."""
    cte = build_seed_entity_cte(
        query='chimera',
        ner_model=None,
        query_embedding=FAKE_EMBEDDING,
        vault_id=FAKE_VAULT_ID,
        enable_semantic_seeding=False,
    )
    assert isinstance(cte, CTE)
    sql = str(cte.compile())
    assert 'UNION ALL' not in sql


def test_semantic_weight_lower_than_ner_weight():
    """Semantic weight (0.7) < NER weight (1.0) in SQL compiled params."""
    cte = build_seed_entity_cte(
        query='chimera',
        ner_model=None,
        query_embedding=FAKE_EMBEDDING,
        vault_id=FAKE_VAULT_ID,
        semantic_seed_weight=0.7,
        enable_semantic_seeding=True,
    )
    compiled = cte.compile()
    params = compiled.params
    # NER seeds use literal 1.0, semantic seeds use literal 0.7
    param_values = list(params.values())
    assert 1.0 in param_values, f'Expected 1.0 (NER weight) in params: {params}'
    assert 0.7 in param_values, f'Expected 0.7 (semantic weight) in params: {params}'


def test_graph_strategy_passes_semantic_params():
    """EntityCooccurrenceGraphStrategy passes semantic seeding params."""
    strategy = EntityCooccurrenceGraphStrategy(
        enable_semantic_seeding=True,
        semantic_seed_top_k=10,
        semantic_seed_weight=0.5,
    )
    vault_ids = [FAKE_VAULT_ID]
    stmt = strategy.get_statement(
        'Who recommended the park?',
        FAKE_EMBEDDING,
        vault_ids=vault_ids,
    )
    sql = str(stmt.compile())
    # Should contain UNION ALL from semantic seeding
    assert 'UNION ALL' in sql
    # Should use seed_entities.weight in scoring (not literal 1.0)
    assert 'seed_entities' in sql


def test_note_graph_strategy_passes_semantic_params():
    """EntityCooccurrenceNoteGraphStrategy passes semantic seeding params."""
    strategy = EntityCooccurrenceNoteGraphStrategy(
        enable_semantic_seeding=True,
        semantic_seed_top_k=10,
        semantic_seed_weight=0.5,
    )
    vault_ids = [FAKE_VAULT_ID]
    stmt = strategy.get_statement(
        'Who recommended the park?',
        FAKE_EMBEDDING,
        vault_ids=vault_ids,
    )
    sql = str(stmt.compile())
    assert 'UNION ALL' in sql
    assert 'doc_graph_seed_entities' in sql


def test_graph_strategy_without_embedding_no_semantic():
    """EntityCooccurrenceGraphStrategy without embedding does not include semantic seeds."""
    strategy = EntityCooccurrenceGraphStrategy(enable_semantic_seeding=True)
    stmt = strategy.get_statement('chimera', None)
    sql = str(stmt.compile())
    # No UNION ALL from semantic seeding -- only the standard UNION ALL
    # between 1st and 2nd order results
    assert 'seed_entities' in sql
    # The seed_entities CTE should not contain semantic_seeds
    assert 'semantic_seeds' not in sql


def test_config_fields_exist_with_correct_defaults():
    """RetrievalConfig has the 3 new graph_semantic_* fields with correct defaults."""
    config = RetrievalConfig()
    assert config.graph_semantic_seeding is True
    assert config.graph_semantic_seed_top_k == 5
    assert config.graph_semantic_seed_weight == 0.7


# ---------------------------------------------------------------------------
# Edge cases and negative tests
# ---------------------------------------------------------------------------


def test_build_seed_entity_cte_no_embedding_no_vault():
    """query_embedding=None and vault_id=None: no semantic seeds, no error."""
    cte = build_seed_entity_cte(
        query='test query',
        ner_model=None,
        query_embedding=None,
        vault_id=None,
        enable_semantic_seeding=True,
    )
    assert isinstance(cte, CTE)
    sql = str(cte.compile())
    # No semantic seeds should be generated without embedding
    assert 'semantic_seeds' not in sql


def test_build_semantic_seed_cte_top_k_zero():
    """top_k=0 produces a CTE that would return no rows."""
    cte = build_semantic_seed_cte(
        query_embedding=FAKE_EMBEDDING,
        vault_id=FAKE_VAULT_ID,
        top_k=0,
        weight=0.7,
    )
    assert isinstance(cte, CTE)


def test_build_semantic_seed_cte_weight_zero():
    """weight=0.0 produces a CTE with zero-weight seeds."""
    cte = build_semantic_seed_cte(
        query_embedding=FAKE_EMBEDDING,
        vault_id=FAKE_VAULT_ID,
        weight=0.0,
    )
    compiled = cte.compile()
    params = compiled.params
    param_values = list(params.values())
    assert 0.0 in param_values


def test_build_semantic_seed_cte_weight_one():
    """weight=1.0 makes semantic seeds equal to NER seeds."""
    cte = build_semantic_seed_cte(
        query_embedding=FAKE_EMBEDDING,
        vault_id=FAKE_VAULT_ID,
        weight=1.0,
    )
    compiled = cte.compile()
    params = compiled.params
    param_values = list(params.values())
    assert 1.0 in param_values


def test_graph_strategy_semantic_disabled_no_semantic_cte():
    """enable_semantic_seeding=False with embedding: no semantic_seeds CTE."""
    strategy = EntityCooccurrenceGraphStrategy(
        enable_semantic_seeding=False,
    )
    stmt = strategy.get_statement(
        'test query',
        FAKE_EMBEDDING,
        vault_ids=[FAKE_VAULT_ID],
    )
    sql = str(stmt.compile())
    assert 'semantic_seeds' not in sql


def test_empty_embedding_list():
    """An empty embedding list is accepted by build_semantic_seed_cte."""
    cte = build_semantic_seed_cte(
        query_embedding=[],
        vault_id=FAKE_VAULT_ID,
    )
    assert isinstance(cte, CTE)
