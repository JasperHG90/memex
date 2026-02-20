from memex_core.memory.retrieval.strategies import DocumentGraphStrategy


def test_document_graph_strategy_returns_union():
    """DocumentGraphStrategy produces a UNION ALL of 1st and 2nd order results."""
    strategy = DocumentGraphStrategy()
    stmt = strategy.get_statement('chimera', None)

    sql = str(stmt.compile())
    assert 'UNION ALL' in sql
    assert 'chunks' in sql


def test_document_graph_strategy_vault_filter():
    """Vault IDs propagate into the generated SQL."""
    from uuid import uuid4

    strategy = DocumentGraphStrategy()
    vault = uuid4()
    stmt = strategy.get_statement('test entity', None, vault_ids=[vault])

    sql = str(stmt.compile())
    assert 'vault_id' in sql


def test_document_graph_strategy_seed_entities_fallback():
    """Without NER, seed entities fall back to similarity search."""
    strategy = DocumentGraphStrategy(ner_model=None)
    stmt = strategy.get_statement('Python', None)

    sql = str(stmt.compile())
    # Fallback uses ilike or similarity
    assert 'similarity' in sql or 'LIKE' in sql


def test_document_graph_strategy_with_ner():
    """With a mock NER model, seed entities use NER-extracted names."""

    class MockNER:
        def predict(self, text: str) -> list[dict[str, str]]:
            return [{'word': 'Python'}, {'word': 'Django'}]

    strategy = DocumentGraphStrategy(ner_model=MockNER())  # type: ignore[arg-type]
    stmt = strategy.get_statement('Tell me about Python and Django', None)

    sql = str(stmt.compile())
    assert 'UNION ALL' in sql
    assert 'chunks' in sql
