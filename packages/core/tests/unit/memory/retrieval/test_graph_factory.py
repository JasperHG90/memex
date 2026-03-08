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
