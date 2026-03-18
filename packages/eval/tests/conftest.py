"""Shared fixtures for eval tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from memex_common.schemas import EntityDTO, MemoryUnitDTO, NoteSearchResult

from memex_eval.internal.scenarios import GroundTruthCheck


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_unit(text: str, fact_type: str = 'world') -> MemoryUnitDTO:
    return MemoryUnitDTO(
        id=uuid4(),
        text=text,
        fact_type=fact_type,
        status='active',
    )


def _make_note_result(texts: list[str], score: float = 0.9) -> NoteSearchResult:
    """Create a NoteSearchResult. For backward compat, texts are joined into the 'what' summary."""
    from memex_common.schemas import SectionSummaryDTO

    summary = SectionSummaryDTO(what=' '.join(texts)) if texts else None
    return NoteSearchResult(
        note_id=uuid4(),
        metadata={},
        summary=summary,
        score=score,
    )


def _make_entity(name: str, entity_type: str | None = None) -> EntityDTO:
    return EntityDTO(
        id=uuid4(),
        name=name,
        mention_count=1,
        entity_type=entity_type,
    )


def _make_check(**overrides) -> GroundTruthCheck:
    defaults = {
        'name': 'test_check',
        'description': 'Test check description',
        'query': 'test query',
        'check_type': 'keyword_in_results',
        'expected': 'test keyword',
    }
    defaults.update(overrides)
    return GroundTruthCheck(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_unit():
    return _make_unit


@pytest.fixture()
def make_note_result():
    return _make_note_result


@pytest.fixture()
def make_entity():
    return _make_entity


@pytest.fixture()
def make_check():
    return _make_check
