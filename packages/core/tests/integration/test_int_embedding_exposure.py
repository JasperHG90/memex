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


# --------------------------------------------------------------------------- #
# HTTP-layer matrix: include_vectors over the real app + real Postgres
# --------------------------------------------------------------------------- #


@pytest.fixture
async def http_client(api):
    """ASGI client on the test's own event loop — a sync TestClient would run
    requests on a second loop and trip the session-scoped async engine."""
    import httpx

    from memex_core.server import app
    from memex_core.server.auth import get_auth_context

    app.state.api = api
    app.dependency_overrides[get_auth_context] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        yield client
    if hasattr(app.state, 'api'):
        del app.state.api
    app.dependency_overrides.pop(get_auth_context, None)


async def _seed_unit(metastore) -> tuple[uuid.UUID, uuid.UUID]:
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'http_matrix')
        note = Note(
            id=uuid.uuid4(),
            vault_id=vault_id,
            title='HTTP matrix note',
            original_text=f'Note {uuid.uuid4()}',
            content_hash=f'hash_{uuid.uuid4()}',
        )
        session.add(note)
        await session.flush()
        unit_id = uuid.uuid4()
        session.add(
            MemoryUnit(
                id=unit_id,
                vault_id=vault_id,
                note_id=note.id,
                text=f'HTTP matrix unit {uuid.uuid4()}',
                fact_type='world',
                embedding=[0.3] * 384,
                event_date=datetime.now(timezone.utc),
            )
        )
        await session.commit()
    return vault_id, unit_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_unit_getters_vector_matrix(http_client, metastore):
    """Rows in Postgres carry vectors; the wire strips them unless requested."""
    vault_id, unit_id = await _seed_unit(metastore)

    resp = await http_client.get(f'/api/v1/memories/{unit_id}')
    assert resp.status_code == 200, resp.text
    assert resp.json()['embedding'] is None

    resp = await http_client.get(f'/api/v1/memories/{unit_id}?include_vectors=true')
    assert resp.status_code == 200
    vec = resp.json()['embedding']
    assert vec is not None and len(vec) == 384
    assert vec[0] == pytest.approx(0.3, abs=1e-4)

    body = {'unit_ids': [str(unit_id)], 'vault_id': str(vault_id)}
    resp = await http_client.post('/api/v1/memories/by-ids', json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]['embedding'] is None

    resp = await http_client.post('/api/v1/memories/by-ids', json={**body, 'include_vectors': True})
    assert resp.status_code == 200
    assert len(resp.json()[0]['embedding']) == 384


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_kv_vector_matrix(http_client, api):
    """KV rows are fully loaded server-side (from_attributes leak surface) —
    the wire must strip by default on get, list, and search."""
    key = f'project:test:embed-matrix-{uuid.uuid4().hex[:8]}'
    await api.kv_put(key=key, value='vector-laden', embedding=[0.7] * 384)

    resp = await http_client.get('/api/v1/kv/get', params={'key': key})
    assert resp.status_code == 200, resp.text
    assert resp.json()['embedding'] is None

    resp = await http_client.get('/api/v1/kv/get', params={'key': key, 'include_vectors': True})
    assert resp.status_code == 200
    assert len(resp.json()['embedding']) == 384

    resp = await http_client.get('/api/v1/kv', params={'key_prefix': key})
    assert resp.status_code == 200
    assert resp.json()[0]['embedding'] is None

    resp = await http_client.get('/api/v1/kv', params={'key_prefix': key, 'include_vectors': True})
    assert resp.status_code == 200
    assert len(resp.json()[0]['embedding']) == 384

    search_body = {'query_embedding': [0.7] * 384, 'limit': 5}
    resp = await http_client.post('/api/v1/kv/search', json=search_body)
    assert resp.status_code == 200
    assert all(r['embedding'] is None for r in resp.json())

    resp = await http_client.post(
        '/api/v1/kv/search', json={**search_body, 'include_vectors': True}
    )
    assert resp.status_code == 200
    hits = [r for r in resp.json() if r['key'] == key]
    assert hits and len(hits[0]['embedding']) == 384


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_vault_summary_vector_matrix(http_client, metastore):
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'http_summary')
        session.add(
            VaultSummary(
                vault_id=vault_id,
                narrative='HTTP matrix narrative.',
                embedding=[0.6] * 384,
            )
        )
        await session.commit()

    resp = await http_client.get(f'/api/v1/vaults/{vault_id}/summary')
    assert resp.status_code == 200, resp.text
    assert resp.json()['embedding'] is None

    resp = await http_client.get(f'/api/v1/vaults/{vault_id}/summary?include_vectors=true')
    assert resp.status_code == 200
    vec = resp.json()['embedding']
    assert vec is not None and len(vec) == 384
    assert vec[0] == pytest.approx(0.6, abs=1e-4)
