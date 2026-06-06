"""Integration tests for stored-embedding exposure (real Postgres + pgvector).

Covers the DB-level truths behind the ``include_vectors`` surfaces:

- vault-summary narrative embedding is persisted on regeneration and
  round-trips through pgvector
- encode failure persists the narrative WITHOUT a vector (non-fatal)
- emptying a vault NULLs a stale narrative embedding
- the by-ids batch lookup is vault-scoped, deduplicates, and omits
  foreign-vault IDs silently

Requires Docker/Postgres via testcontainers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlmodel import col, select

from memex_common.config import VaultSummaryConfig
from memex_core.memory.sql_models import MemoryUnit, Note, VaultSummary
from memex_core.services.vault_summary import VaultSummaryService


async def _create_vault(session, name_prefix: str) -> uuid.UUID:
    vault_id = uuid.uuid4()
    await session.execute(
        text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
        {'id': str(vault_id), 'name': f'{name_prefix}_{vault_id.hex[:8]}'},
    )
    return vault_id


def _summary_service(metastore, embedding_model) -> VaultSummaryService:
    return VaultSummaryService(
        metastore=metastore,
        lm=MagicMock(),
        config=VaultSummaryConfig(),
        embedding_model=embedding_model,
    )


async def _read_summary(metastore, vault_id) -> VaultSummary | None:
    async with metastore.session() as session:
        result = await session.execute(
            select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
        )
        return result.scalar_one_or_none()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_regenerate_persists_narrative_embedding(metastore, mock_embedding_model):
    """Regeneration encodes the final narrative and persists the vector."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'emb_regen')
        session.add(
            Note(
                id=uuid.uuid4(),
                vault_id=vault_id,
                title='Embedding exposure design notes',
                original_text=f'Note about embeddings {uuid.uuid4()}',
                content_hash=f'hash_{uuid.uuid4()}',
            )
        )
        await session.commit()

    service = _summary_service(metastore, mock_embedding_model)
    fake_prediction = SimpleNamespace(narrative='A vault about embedding exposure.', themes=[])
    with patch(
        'memex_core.services.vault_summary.run_dspy_operation',
        new=AsyncMock(return_value=fake_prediction),
    ):
        returned = await service.regenerate_summary(vault_id)

    assert returned.embedding is not None
    row = await _read_summary(metastore, vault_id)
    assert row is not None
    assert row.narrative == 'A vault about embedding exposure.'
    assert row.embedding is not None
    assert len(list(row.embedding)) == 384
    assert list(row.embedding)[0] == pytest.approx(0.1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_encode_failure_persists_narrative_without_vector(metastore):
    """A broken embedding backend must not fail the summary write."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'emb_fail')
        session.add(
            Note(
                id=uuid.uuid4(),
                vault_id=vault_id,
                title='Encode failure scenario',
                original_text=f'Note {uuid.uuid4()}',
                content_hash=f'hash_{uuid.uuid4()}',
            )
        )
        await session.commit()

    broken_model = MagicMock()
    broken_model.encode.side_effect = RuntimeError('encode exploded')
    service = _summary_service(metastore, broken_model)
    fake_prediction = SimpleNamespace(narrative='Narrative survives.', themes=[])
    with patch(
        'memex_core.services.vault_summary.run_dspy_operation',
        new=AsyncMock(return_value=fake_prediction),
    ):
        returned = await service.regenerate_summary(vault_id)

    assert returned.narrative == 'Narrative survives.'
    row = await _read_summary(metastore, vault_id)
    assert row is not None
    assert row.narrative == 'Narrative survives.'
    assert row.embedding is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_vault_nulls_stale_embedding(metastore, mock_embedding_model):
    """Emptying a vault must not leave the old narrative's vector behind."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'emb_empty')
        session.add(
            VaultSummary(
                vault_id=vault_id,
                narrative='Old narrative with a vector.',
                embedding=[0.5] * 384,
            )
        )
        await session.commit()

    service = _summary_service(metastore, mock_embedding_model)
    # No notes in the vault -> regenerate routes to _create_empty_summary.
    returned = await service.regenerate_summary(vault_id)

    assert returned.narrative == 'This vault is empty.'
    row = await _read_summary(metastore, vault_id)
    assert row is not None
    assert row.embedding is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_summary_embedding_pgvector_roundtrip(metastore):
    """The 384-dim vector survives a write/read cycle through pgvector."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'emb_roundtrip')
        session.add(
            VaultSummary(
                vault_id=vault_id,
                narrative='Roundtrip narrative.',
                embedding=[0.25] * 384,
            )
        )
        await session.commit()

    row = await _read_summary(metastore, vault_id)
    assert row is not None
    values = list(row.embedding)
    assert len(values) == 384
    assert values[0] == pytest.approx(0.25)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_memory_units_by_ids_is_vault_scoped(api, metastore):
    """Foreign-vault IDs are silently omitted; duplicates deduplicate."""
    async with metastore.session() as session:
        vault_a = await _create_vault(session, 'byids_a')
        vault_b = await _create_vault(session, 'byids_b')
        note_a = Note(
            id=uuid.uuid4(),
            vault_id=vault_a,
            original_text=f'Note A {uuid.uuid4()}',
            content_hash=f'hash_{uuid.uuid4()}',
        )
        note_b = Note(
            id=uuid.uuid4(),
            vault_id=vault_b,
            original_text=f'Note B {uuid.uuid4()}',
            content_hash=f'hash_{uuid.uuid4()}',
        )
        session.add(note_a)
        session.add(note_b)
        await session.flush()

        unit_a_id, unit_b_id = uuid.uuid4(), uuid.uuid4()
        unit_a = MemoryUnit(
            id=unit_a_id,
            vault_id=vault_a,
            note_id=note_a.id,
            text=f'Unit in vault A {uuid.uuid4()}',
            fact_type='world',
            embedding=[0.1] * 384,
            event_date=datetime.now(timezone.utc),
        )
        unit_b = MemoryUnit(
            id=unit_b_id,
            vault_id=vault_b,
            note_id=note_b.id,
            text=f'Unit in vault B {uuid.uuid4()}',
            fact_type='world',
            embedding=[0.2] * 384,
            event_date=datetime.now(timezone.utc),
        )
        session.add(unit_a)
        session.add(unit_b)
        await session.commit()

    # Duplicate unit_a_id + a foreign-vault id + a nonexistent id.
    results = await api.get_memory_units_by_ids(
        [unit_a_id, unit_a_id, unit_b_id, uuid.uuid4()],
        vault_a,
    )

    assert [u.id for u in results] == [unit_a_id]
    # The eager row exposes its vector in-process regardless of any flag.
    assert results[0].embedding is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_memory_units_by_ids_empty_input(api):
    assert await api.get_memory_units_by_ids([], uuid.uuid4()) == []
