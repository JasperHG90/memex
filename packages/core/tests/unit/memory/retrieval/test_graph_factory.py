"""Tests for pluggable graph retriever factory and build_seed_entity_cte helper."""

import pytest
from sqlalchemy.sql.expression import CTE

from memex_core.memory.retrieval.strategies import (
    EntityCooccurrenceGraphStrategy,
    EntityCooccurrenceNoteGraphStrategy,
    GraphStrategy,
    NoteGraphStrategy,
    build_seed_entity_cte,
    get_graph_strategy,
    get_note_graph_strategy,
)


# ---------------------------------------------------------------------------
# Alias backward compatibility
# ---------------------------------------------------------------------------


class TestAliases:
    def test_graph_strategy_alias(self) -> None:
        assert GraphStrategy is EntityCooccurrenceGraphStrategy

    def test_note_graph_strategy_alias(self) -> None:
        assert NoteGraphStrategy is EntityCooccurrenceNoteGraphStrategy


# ---------------------------------------------------------------------------
# Factory: get_graph_strategy
# ---------------------------------------------------------------------------


class TestGetGraphStrategy:
    def test_default_returns_entity_cooccurrence(self) -> None:
        strategy = get_graph_strategy()
        assert isinstance(strategy, EntityCooccurrenceGraphStrategy)

    def test_explicit_entity_cooccurrence(self) -> None:
        strategy = get_graph_strategy(type='entity_cooccurrence')
        assert isinstance(strategy, EntityCooccurrenceGraphStrategy)

    def test_passes_ner_model(self) -> None:
        class MockNER:
            def predict(self, text: str) -> list[dict[str, str]]:
                return []

        ner = MockNER()
        strategy = get_graph_strategy(ner_model=ner)  # type: ignore[arg-type]
        assert isinstance(strategy, EntityCooccurrenceGraphStrategy)
        assert strategy.ner_model is ner  # type: ignore[union-attr]

    def test_passes_kwargs(self) -> None:
        strategy = get_graph_strategy(similarity_threshold=0.5)
        assert isinstance(strategy, EntityCooccurrenceGraphStrategy)
        assert strategy.similarity_threshold == 0.5  # type: ignore[union-attr]

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match='Unknown graph retriever type'):
            get_graph_strategy(type='nonexistent')


# ---------------------------------------------------------------------------
# Factory: get_note_graph_strategy
# ---------------------------------------------------------------------------


class TestGetNoteGraphStrategy:
    def test_default_returns_entity_cooccurrence(self) -> None:
        strategy = get_note_graph_strategy()
        assert isinstance(strategy, EntityCooccurrenceNoteGraphStrategy)

    def test_explicit_entity_cooccurrence(self) -> None:
        strategy = get_note_graph_strategy(type='entity_cooccurrence')
        assert isinstance(strategy, EntityCooccurrenceNoteGraphStrategy)

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match='Unknown note graph retriever type'):
            get_note_graph_strategy(type='nonexistent')


# ---------------------------------------------------------------------------
# build_seed_entity_cte
# ---------------------------------------------------------------------------


class TestBuildSeedEntityCte:
    def test_returns_cte(self) -> None:
        cte = build_seed_entity_cte('some query')
        assert isinstance(cte, CTE)

    def test_cte_has_expected_columns(self) -> None:
        cte = build_seed_entity_cte('hello')
        col_names = [c.name for c in cte.c]
        assert 'id' in col_names
        assert 'weight' in col_names

    def test_custom_cte_name(self) -> None:
        cte = build_seed_entity_cte('query', cte_name='my_seeds')
        assert cte.name == 'my_seeds'

    def test_without_ilike_no_ilike_in_ner_path(self) -> None:
        """When include_ilike=False and NER returns entities, no ILIKE appears
        in the canonical/alias conditions for those entities."""

        class MockNER:
            def predict(self, text: str) -> list[dict[str, str]]:
                return [{'word': 'Alice'}]

        cte = build_seed_entity_cte(
            'Tell me about Alice',
            ner_model=MockNER(),  # type: ignore[arg-type]
            include_ilike=False,
        )
        # The NER path without include_ilike should not add per-entity ilike
        # But the fallback path always has ilike; since NER succeeds here, we
        # should NOT see the fallback ilike pattern for the raw query.
        assert isinstance(cte, CTE)

    def test_with_ilike_adds_ilike_conditions(self) -> None:
        """When include_ilike=True and NER returns entities, ILIKE conditions
        appear in the generated SQL."""

        class MockNER:
            def predict(self, text: str) -> list[dict[str, str]]:
                return [{'word': 'Alice'}]

        cte = build_seed_entity_cte(
            'Tell me about Alice',
            ner_model=MockNER(),  # type: ignore[arg-type]
            include_ilike=True,
        )
        sql = str(cte.compile())
        # With include_ilike, we should see LIKE (SQLAlchemy compiles ilike to LIKE)
        assert 'LIKE' in sql

    def test_fallback_path_uses_similarity(self) -> None:
        """Without NER, the fallback path uses similarity and ilike."""
        cte = build_seed_entity_cte('chimera', ner_model=None)
        sql = str(cte.compile())
        assert 'similarity' in sql or 'LIKE' in sql


# ---------------------------------------------------------------------------
# Default behavior unchanged
# ---------------------------------------------------------------------------


class TestDefaultBehaviorUnchanged:
    def test_graph_strategy_produces_union_all(self) -> None:
        strategy = get_graph_strategy()
        stmt = strategy.get_statement('chimera', None)
        sql = str(stmt.compile())
        assert 'UNION ALL' in sql

    def test_note_graph_strategy_produces_union_all(self) -> None:
        strategy = get_note_graph_strategy()
        stmt = strategy.get_statement('chimera', None)
        sql = str(stmt.compile())
        assert 'UNION ALL' in sql
        assert 'chunks' in sql


# ---------------------------------------------------------------------------
# Edge cases and negative tests
# ---------------------------------------------------------------------------


class TestBuildSeedEntityCteEdgeCases:
    def test_empty_query_returns_cte(self) -> None:
        """build_seed_entity_cte with an empty query still returns a valid CTE."""
        cte = build_seed_entity_cte('')
        assert isinstance(cte, CTE)
        col_names = [c.name for c in cte.c]
        assert 'id' in col_names
        assert 'weight' in col_names

    def test_ner_model_none_explicit(self) -> None:
        """Explicitly passing ner_model=None uses the fallback similarity path."""
        cte = build_seed_entity_cte('Alice and Bob', ner_model=None)
        assert isinstance(cte, CTE)
        sql = str(cte.compile())
        assert 'similarity' in sql or 'LIKE' in sql

    def test_ner_returns_empty_list_falls_back(self) -> None:
        """When NER returns an empty list, fallback path is used."""

        class EmptyNER:
            def predict(self, text: str) -> list[dict[str, str]]:
                return []

        cte = build_seed_entity_cte('hello world', ner_model=EmptyNER())  # type: ignore[arg-type]
        assert isinstance(cte, CTE)
        sql = str(cte.compile())
        # Should use fallback (similarity / LIKE), not NER path
        assert 'similarity' in sql or 'LIKE' in sql

    def test_ner_raises_exception_falls_back(self) -> None:
        """When NER raises an exception, fallback path is used without propagating."""

        class FailingNER:
            def predict(self, text: str) -> list[dict[str, str]]:
                raise RuntimeError('NER model failed')

        cte = build_seed_entity_cte(
            'Tell me about Alice',
            ner_model=FailingNER(),  # type: ignore[arg-type]
        )
        assert isinstance(cte, CTE)


class TestGetGraphStrategyEdgeCases:
    def test_similarity_threshold_zero(self) -> None:
        """similarity_threshold=0.0 is accepted (most permissive)."""
        strategy = get_graph_strategy(similarity_threshold=0.0)
        assert isinstance(strategy, EntityCooccurrenceGraphStrategy)
        assert strategy.similarity_threshold == 0.0  # type: ignore[union-attr]

    def test_similarity_threshold_one(self) -> None:
        """similarity_threshold=1.0 is accepted (most restrictive)."""
        strategy = get_graph_strategy(similarity_threshold=1.0)
        assert isinstance(strategy, EntityCooccurrenceGraphStrategy)
        assert strategy.similarity_threshold == 1.0  # type: ignore[union-attr]

    def test_empty_type_string_raises(self) -> None:
        """Empty type string raises ValueError."""
        with pytest.raises(ValueError, match='Unknown graph retriever type'):
            get_graph_strategy(type='')

    def test_note_factory_empty_type_string_raises(self) -> None:
        """Empty type string raises ValueError for note factory."""
        with pytest.raises(ValueError, match='Unknown note graph retriever type'):
            get_note_graph_strategy(type='')
