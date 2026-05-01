"""F20 unit: cross-vault review is rejected (Wave 0 vault-scoping invariant).

The MCP brief mandates ``vault_id`` as a REQUIRED parameter on
``memex_memory_review`` so the service can enforce that the unit being
reviewed actually belongs to the caller's vault. This test pins the
service-layer guard: a unit whose ``vault_id`` differs from the caller's
``vault_id`` raises ``PermissionError`` BEFORE any FSRS schedule advance,
counter write, or audit emission.

Without this guard, a caller in vault A could review a unit in vault B
just by knowing its UUID — the row-lock would succeed, FSRS would
advance, the outcome counters would mutate, and an audit row would land,
all under the wrong vault's scope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.memory.revisit import Quality
from memex_core.services.revisitation import RevisitationService


@pytest.mark.asyncio
async def test_review_rejects_cross_vault_with_permission_error() -> None:
    caller_vault = uuid4()
    unit_vault = uuid4()
    unit_id = uuid4()

    unit = SimpleNamespace(
        id=unit_id,
        vault_id=unit_vault,
        revisit_stability=None,
        revisit_difficulty=None,
        revisit_due_at=None,
        revisit_review_count=0,
        is_deprioritized=False,
    )

    session = MagicMock()
    session.get = AsyncMock(return_value=unit)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    class _SessionCtx:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    metastore = MagicMock()
    metastore.session = MagicMock(return_value=_SessionCtx())

    service = RevisitationService(metastore, MagicMock(), MagicMock())

    with pytest.raises(PermissionError, match='does not belong to vault'):
        await service.review(
            unit_id,
            Quality.GOOD,
            vault_id=caller_vault,
            now=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )

    session.commit.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.add.assert_not_called()
