"""Tests for vault summary integration with truncation."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


class TestTruncateVaultCascade:
    @pytest.mark.asyncio
    async def test_truncate_deletes_vault_summary(self):
        """AC-A07: truncate_vault deletes the vault's VaultSummary."""
        from memex_core.services.vaults import VaultService

        metastore = MagicMock()
        filestore = AsyncMock()
        config = MagicMock()

        svc = VaultService(metastore=metastore, filestore=filestore, config=config)
        vault_id = uuid4()

        # Mock session
        session = AsyncMock()

        # Mock note query (no notes)
        note_result = MagicMock()
        note_result.all.return_value = []

        # Mock orphan entity query (no orphans)
        orphan_result = MagicMock()
        orphan_result.all.return_value = []

        # Mock delete results
        delete_result = MagicMock()
        delete_result.rowcount = 0

        session.exec = AsyncMock(side_effect=[note_result, orphan_result] + [delete_result] * 6)
        session.commit = AsyncMock()

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        svc.metastore.session.return_value = ctx

        counts = await svc.truncate_vault(vault_id)

        # Verify vault_summaries is in the counts
        assert 'vault_summaries' in counts
