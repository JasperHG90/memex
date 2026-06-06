"""Unit tests for the vault-summary narrative embedding helper.

``_embed_narrative`` must be non-fatal: a summary without a vector beats no
summary, so encode failures log a warning and return None instead of
propagating into the persist path.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from memex_common.config import VaultSummaryConfig
from memex_core.services.vault_summary import VaultSummaryService


class _FakeVector:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return list(self._values)


def _service(embedding_model) -> VaultSummaryService:
    return VaultSummaryService(
        metastore=MagicMock(),
        lm=MagicMock(),
        config=VaultSummaryConfig(),
        embedding_model=embedding_model,
    )


@pytest.mark.asyncio
async def test_embed_narrative_returns_vector() -> None:
    model = MagicMock()
    model.encode.return_value = [_FakeVector([0.1, 0.2, 0.3])]
    service = _service(model)

    result = await service._embed_narrative('A vault about embeddings.', uuid4())

    assert result == pytest.approx([0.1, 0.2, 0.3])
    model.encode.assert_called_once_with(['A vault about embeddings.'])


@pytest.mark.asyncio
async def test_embed_narrative_failure_is_non_fatal(caplog: pytest.LogCaptureFixture) -> None:
    model = MagicMock()
    model.encode.side_effect = RuntimeError('onnx session crashed')
    service = _service(model)
    vault_id = uuid4()

    with caplog.at_level(logging.WARNING, logger='memex.core.services.vault_summary'):
        result = await service._embed_narrative('whatever', vault_id)

    assert result is None
    assert any('persisting without vector' in r.message for r in caplog.records)
    assert any(str(vault_id) in r.message for r in caplog.records)


def test_constructor_requires_embedding_model() -> None:
    """The embedding model is a required collaborator, not an optional extra."""
    with pytest.raises(TypeError):
        VaultSummaryService(  # type: ignore[call-arg]
            metastore=MagicMock(),
            lm=MagicMock(),
            config=VaultSummaryConfig(),
        )
