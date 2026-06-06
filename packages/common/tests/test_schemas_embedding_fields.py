"""Wire-schema pins for opt-in vector exposure.

Three DTOs gained an ``embedding`` field that defaults to None (so agent
surfaces and existing callers see no vectors unless explicitly requested),
and the dead ``RetrievalRequest.include_vectors`` flag was deleted (search
results are vector-free by design — dead-flag-or-wired, never dead-flag-kept).
"""

from __future__ import annotations

import pytest

from memex_common.schemas import (
    KVEntryDTO,
    KVSearchRequest,
    MemoryUnitDTO,
    RetrievalRequest,
    VaultSummaryDTO,
)


class TestEmbeddingFieldDefaults:
    def test_memory_unit_dto_embedding_defaults_none(self) -> None:
        field = MemoryUnitDTO.model_fields['embedding']
        assert field.default is None

    def test_kv_entry_dto_embedding_defaults_none(self) -> None:
        field = KVEntryDTO.model_fields['embedding']
        assert field.default is None

    def test_vault_summary_dto_embedding_defaults_none(self) -> None:
        field = VaultSummaryDTO.model_fields['embedding']
        assert field.default is None


class TestDeadFlagDeleted:
    def test_retrieval_request_has_no_include_vectors(self) -> None:
        """The flag was dead for its whole life (zero readers); search stays
        vector-free, so the field must not resurface on the wire model."""
        assert 'include_vectors' not in RetrievalRequest.model_fields


class TestKVSearchRequestIncludeVectors:
    def test_defaults_false(self) -> None:
        req = KVSearchRequest(query='what editor do I use?')
        assert req.include_vectors is False

    def test_exactly_one_query_validator_still_enforced(self) -> None:
        with pytest.raises(ValueError):
            KVSearchRequest(query='text', query_embedding=[0.1, 0.2], include_vectors=True)

    def test_accepts_include_vectors_with_query_embedding(self) -> None:
        req = KVSearchRequest(query_embedding=[0.1, 0.2], include_vectors=True)
        assert req.include_vectors is True
