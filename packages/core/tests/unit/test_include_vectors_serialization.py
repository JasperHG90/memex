"""Serialization-boundary tests for opt-in vector exposure.

Pins the three server-side strip points and the dead-flag deletion:

- ``build_memory_unit_dto`` only reads ``embedding`` under ``include_vectors``
- ``_kv_entry_dto`` strips the auto-populated vector unless requested
  (``model_validate(from_attributes=True)`` would otherwise leak it on
  every KV response)
- ``_summary_to_dto`` populates the narrative embedding only on request
- the internal retrieval request model no longer carries ``include_vectors``
  (search results are vector-free by design)
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from memex_core.memory.retrieval.models import RetrievalRequest as InternalRetrievalRequest
from memex_core.memory.sql_models import VaultSummary
from memex_core.server.common import build_memory_unit_dto, vector_to_list
from memex_core.server.kv import _kv_entry_dto
from memex_core.server.vault_summary import _summary_to_dto


class _FakeVector:
    """Stands in for a numpy/pgvector array: iterable with ``tolist``."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return list(self._values)


def _unit(embedding):
    return SimpleNamespace(
        id=uuid4(),
        note_id=str(uuid4()),
        vault_id=str(uuid4()),
        chunk_id=uuid4(),
        text='unit text',
        fact_type='world',
        status='active',
        mentioned_at=None,
        event_date=None,
        occurred_start=None,
        occurred_end=None,
        unit_metadata={},
        confidence=1.0,
        embedding=embedding,
    )


class TestVectorToList:
    def test_none_passthrough(self) -> None:
        assert vector_to_list(None) is None

    def test_plain_list(self) -> None:
        assert vector_to_list([0.1, 0.2]) == [0.1, 0.2]

    def test_tolist_object(self) -> None:
        assert vector_to_list(_FakeVector([1.0, 2.0])) == [1.0, 2.0]

    def test_generic_iterable(self) -> None:
        assert vector_to_list((0.5, 0.25)) == [0.5, 0.25]


class TestBuildMemoryUnitDtoVectors:
    def test_default_strips_embedding(self) -> None:
        dto = build_memory_unit_dto(_unit(embedding=[0.1, 0.2]))
        assert dto.embedding is None

    def test_include_vectors_populates(self) -> None:
        dto = build_memory_unit_dto(_unit(embedding=[0.1, 0.2]), include_vectors=True)
        assert dto.embedding == pytest.approx([0.1, 0.2])

    def test_include_vectors_converts_array_like(self) -> None:
        dto = build_memory_unit_dto(_unit(embedding=_FakeVector([0.3, 0.4])), include_vectors=True)
        assert dto.embedding == pytest.approx([0.3, 0.4])

    def test_unit_without_embedding_attribute_yields_none(self) -> None:
        unit = _unit(embedding=None)
        del unit.embedding
        dto = build_memory_unit_dto(unit, include_vectors=True)
        assert dto.embedding is None


class TestKvEntryDtoStrip:
    def _entry(self):
        import datetime as dt

        return SimpleNamespace(
            id=uuid4(),
            key='user:editor',
            value='neovim',
            embedding=[0.9, 0.8],
            expires_at=None,
            created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            updated_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
        )

    def test_default_strips_auto_populated_vector(self) -> None:
        """from_attributes auto-populates ``embedding`` — the strip is load-bearing."""
        dto = _kv_entry_dto(self._entry(), include_vectors=False)
        assert dto.embedding is None

    def test_include_vectors_keeps_vector(self) -> None:
        dto = _kv_entry_dto(self._entry(), include_vectors=True)
        assert dto.embedding == pytest.approx([0.9, 0.8])

    def test_entry_without_vector_stays_none(self) -> None:
        entry = self._entry()
        entry.embedding = None
        dto = _kv_entry_dto(entry, include_vectors=True)
        assert dto.embedding is None


class TestVaultSummaryDtoVectors:
    def _summary(self, embedding):
        import datetime as dt

        return VaultSummary(
            id=uuid4(),
            vault_id=uuid4(),
            narrative='A vault about testing.',
            themes=[],
            inventory={},
            key_entities=[],
            embedding=embedding,
            version=3,
            notes_incorporated=5,
            created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            updated_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
        )

    def test_default_strips_embedding(self) -> None:
        dto = _summary_to_dto(self._summary(embedding=[0.1] * 4))
        assert dto.embedding is None

    def test_include_vectors_populates(self) -> None:
        dto = _summary_to_dto(self._summary(embedding=[0.1] * 4), include_vectors=True)
        assert dto.embedding == pytest.approx([0.1] * 4)

    def test_null_column_stays_null_when_requested(self) -> None:
        dto = _summary_to_dto(self._summary(embedding=None), include_vectors=True)
        assert dto.embedding is None


class TestDeadFlagDeleted:
    def test_internal_retrieval_request_has_no_include_vectors(self) -> None:
        """Search is vector-free by design — the dead flag must not resurface."""
        assert 'include_vectors' not in InternalRetrievalRequest.model_fields
