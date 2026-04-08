"""Unit tests for VaultSummaryService needs_regeneration flag behavior."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.memory.sql_models import VaultSummary
from memex_core.services.vault_summary import VaultSummaryService


def _make_service():
    metastore = MagicMock()
    lm = MagicMock()
    config = MagicMock()
    config.max_batch_tokens = 4000
    config.batch_size = 20
    config.max_narrative_tokens = 200
    config.max_patch_log = 20
    return VaultSummaryService(metastore=metastore, lm=lm, config=config)


def _mock_session(metastore, session):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore.session.return_value = ctx


class TestMarkNeedsRegeneration:
    @pytest.mark.asyncio
    async def test_sets_flag_on_existing_summary(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(vault_id=vault_id, needs_regeneration=False)

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = summary
        session.execute = AsyncMock(return_value=result)
        _mock_session(svc.metastore, session)

        await svc.mark_needs_regeneration(vault_id)

        assert summary.needs_regeneration is True
        session.add.assert_called_once_with(summary)
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_when_no_summary_exists(self):
        svc = _make_service()
        vault_id = uuid4()

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        _mock_session(svc.metastore, session)

        await svc.mark_needs_regeneration(vault_id)

        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idempotent_when_already_set(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(vault_id=vault_id, needs_regeneration=True)

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = summary
        session.execute = AsyncMock(return_value=result)
        _mock_session(svc.metastore, session)

        await svc.mark_needs_regeneration(vault_id)

        assert summary.needs_regeneration is True


class TestIsStaleWithRegenerationFlag:
    @pytest.mark.asyncio
    async def test_returns_true_when_needs_regeneration_set(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(vault_id=vault_id, needs_regeneration=True, version=5)

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = summary
        session.execute = AsyncMock(return_value=result)
        _mock_session(svc.metastore, session)

        is_stale = await svc.is_stale(vault_id)

        assert is_stale is True
        # Should return early without querying for unincorporated notes
        assert session.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_falls_through_when_flag_not_set(self):
        svc = _make_service()
        vault_id = uuid4()
        summary = VaultSummary(vault_id=vault_id, needs_regeneration=False, version=5)

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = summary

        # Second call returns count of 0 (no unincorporated notes)
        count_result = MagicMock()
        count_result.scalar.return_value = 0

        session.execute = AsyncMock(side_effect=[result, count_result])
        _mock_session(svc.metastore, session)

        is_stale = await svc.is_stale(vault_id)

        assert is_stale is False
        # Should have made two DB calls (summary fetch + note count)
        assert session.execute.await_count == 2
