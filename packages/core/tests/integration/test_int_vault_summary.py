"""Integration tests for VaultSummary (Feature A, AC-A01/A07).

Requires Docker/Postgres via testcontainers.
"""

import pytest
from uuid import uuid4

from sqlmodel import select, col, text
from memex_core.memory.sql_models import VaultSummary


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_summary_table_exists(metastore):
    """AC-A01: vault_summaries table exists and is queryable."""
    async with metastore.session() as session:
        result = await session.execute(text('SELECT COUNT(*) FROM vault_summaries'))
        count = result.scalar()
        assert count == 0  # Empty table on fresh DB


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_summary_cascade_delete(metastore):
    """AC-A07: Deleting a vault cascade-deletes its VaultSummary."""
    async with metastore.session() as session:
        # Create a vault
        vault_id = uuid4()
        await session.execute(
            text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
            {'id': str(vault_id), 'name': f'test_vault_{vault_id.hex[:8]}'},
        )

        # Create a VaultSummary for that vault
        summary = VaultSummary(vault_id=vault_id, narrative='Test summary')
        session.add(summary)
        await session.commit()

        # Verify it exists
        result = await session.execute(
            select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
        )
        assert result.first() is not None

        # Delete the vault
        await session.execute(text('DELETE FROM vaults WHERE id = :id'), {'id': str(vault_id)})
        await session.commit()

        # Verify cascade deleted the summary
        result = await session.execute(
            select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
        )
        assert result.first() is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_summary_partial_index_exists(metastore):
    """AC-B04: Partial index on memory_units.context exists."""
    async with metastore.session() as session:
        result = await session.execute(
            text(
                'SELECT indexname FROM pg_indexes '
                "WHERE tablename = 'memory_units' AND indexname = 'ix_memory_units_context'"
            )
        )
        row = result.first()
        assert row is not None, 'Partial index ix_memory_units_context does not exist'
