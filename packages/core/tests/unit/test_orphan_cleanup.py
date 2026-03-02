"""Tests for orphaned mental model cleanup in storage.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestCleanupOrphanedMentalModels:
    """Tests for cleanup_orphaned_mental_models SQL logic."""

    @pytest.mark.asyncio
    async def test_cleanup_returns_rowcount(self):
        """Verify the function executes a DELETE and returns rowcount."""
        from unittest.mock import AsyncMock

        session = AsyncMock()
        # Mock the exec result with a rowcount
        mock_result = MagicMock()
        mock_result.rowcount = 3
        session.exec = AsyncMock(return_value=mock_result)

        from memex_core.memory.extraction.storage import cleanup_orphaned_mental_models

        count = await cleanup_orphaned_mental_models(session)

        assert count == 3
        session.exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_entities_returns_rowcount(self):
        """Verify cleanup_orphaned_entities also works correctly."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 5
        session.exec = AsyncMock(return_value=mock_result)

        from memex_core.memory.extraction.storage import cleanup_orphaned_entities

        count = await cleanup_orphaned_entities(session)

        assert count == 5
        session.exec.assert_called_once()
