"""Integration tests for F46 ``get_memory_units_by_chunks`` (chunk→units traversal).

Coverage:

* Returns the memory units extracted from the requested chunks.
* Returns an empty list when no chunk in the input matches.
* Vault-scoping: a memory unit anchored to the same chunk_id but living in a
  different vault is NOT returned (chunk_ids reused across vaults must not
  leak — defensive guard since chunk_id is FK only, no vault constraint at
  the schema level).
* Empty ``chunk_ids`` short-circuits to an empty list.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import pytest

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import Chunk, MemoryUnit, Note, Vault

pytestmark = [pytest.mark.integration]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _seed_chunk(session, *, vault_id: UUID, note_id: UUID, chunk_index: int = 0) -> Chunk:
    chunk = Chunk(
        id=uuid4(),
        vault_id=vault_id,
        note_id=note_id,
        text=f'chunk-{chunk_index}',
        content_hash=f'h-{uuid4().hex[:16]}',
        embedding=[0.1] * 384,
        chunk_index=chunk_index,
    )
    session.add(chunk)
    await session.flush()
    return chunk


async def _seed_unit(
    session, *, vault_id: UUID, note_id: UUID, chunk_id: UUID | None, text: str
) -> MemoryUnit:
    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault_id,
        note_id=note_id,
        chunk_id=chunk_id,
        text=text,
        fact_type='world',
        embedding=[0.1] * 384,
        event_date=_now(),
    )
    session.add(unit)
    await session.flush()
    return unit


@pytest.mark.asyncio
async def test_returns_units_for_known_chunks(api, metastore, init_global_vault):
    """Two chunks yield three memory units; all three are returned."""
    note_id = uuid4()
    async with metastore.session() as session:
        session.add(Note(id=note_id, vault_id=GLOBAL_VAULT_ID, original_text='note body'))
        await session.flush()

        chunk_a = await _seed_chunk(
            session, vault_id=GLOBAL_VAULT_ID, note_id=note_id, chunk_index=0
        )
        chunk_b = await _seed_chunk(
            session, vault_id=GLOBAL_VAULT_ID, note_id=note_id, chunk_index=1
        )

        u1 = await _seed_unit(
            session,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id,
            chunk_id=chunk_a.id,
            text='unit from chunk A first',
        )
        u2 = await _seed_unit(
            session,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id,
            chunk_id=chunk_a.id,
            text='unit from chunk A second',
        )
        u3 = await _seed_unit(
            session,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id,
            chunk_id=chunk_b.id,
            text='unit from chunk B',
        )
        await session.commit()

    units = await api.get_memory_units_by_chunks([chunk_a.id, chunk_b.id], GLOBAL_VAULT_ID)
    returned_ids = {u.id for u in units}
    assert returned_ids == {u1.id, u2.id, u3.id}


@pytest.mark.asyncio
async def test_returns_empty_for_unknown_chunk(api, metastore, init_global_vault):
    """An unknown chunk_id yields no units."""
    units = await api.get_memory_units_by_chunks([uuid4()], GLOBAL_VAULT_ID)
    assert units == []


@pytest.mark.asyncio
async def test_empty_chunk_ids_short_circuits(api, metastore, init_global_vault):
    """An empty chunk_ids list returns an empty result without querying."""
    units = await api.get_memory_units_by_chunks([], GLOBAL_VAULT_ID)
    assert units == []


@pytest.mark.asyncio
async def test_http_route_returns_units(api, metastore, init_global_vault):
    """The ``/api/v1/memories/by-chunks`` route returns the same vault-scoped result."""
    from httpx import ASGITransport, AsyncClient

    from memex_core.server import app

    await api.initialize()
    app.state.api = api

    note_id = uuid4()
    async with metastore.session() as session:
        session.add(Note(id=note_id, vault_id=GLOBAL_VAULT_ID, original_text='note body'))
        await session.flush()
        chunk = await _seed_chunk(session, vault_id=GLOBAL_VAULT_ID, note_id=note_id, chunk_index=0)
        unit = await _seed_unit(
            session,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_id,
            chunk_id=chunk.id,
            text='via http route',
        )
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.post(
            '/api/v1/memories/by-chunks',
            json={'chunk_ids': [str(chunk.id)], 'vault_id': str(GLOBAL_VAULT_ID)},
        )
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert any(row['id'] == str(unit.id) for row in body)
        assert all(row['vault_id'] == str(GLOBAL_VAULT_ID) for row in body)


@pytest.mark.asyncio
async def test_vault_scoping_blocks_other_vault_units(api, metastore, init_global_vault):
    """A unit anchored to the same chunk_id but in a different vault MUST NOT leak."""
    other_vault_id = uuid4()
    note_a = uuid4()
    note_b = uuid4()

    async with metastore.session() as session:
        session.add(Vault(id=other_vault_id, name=f'vault-other-{uuid4().hex[:6]}'))
        await session.flush()
        session.add(Note(id=note_a, vault_id=GLOBAL_VAULT_ID, original_text='a'))
        session.add(Note(id=note_b, vault_id=other_vault_id, original_text='b'))
        await session.flush()

        chunk_global = await _seed_chunk(
            session, vault_id=GLOBAL_VAULT_ID, note_id=note_a, chunk_index=0
        )
        chunk_other = await _seed_chunk(
            session, vault_id=other_vault_id, note_id=note_b, chunk_index=0
        )

        unit_global = await _seed_unit(
            session,
            vault_id=GLOBAL_VAULT_ID,
            note_id=note_a,
            chunk_id=chunk_global.id,
            text='global vault unit',
        )
        unit_other = await _seed_unit(
            session,
            vault_id=other_vault_id,
            note_id=note_b,
            chunk_id=chunk_other.id,
            text='other vault unit',
        )
        await session.commit()

    units_global = await api.get_memory_units_by_chunks(
        [chunk_global.id, chunk_other.id], GLOBAL_VAULT_ID
    )
    returned_ids_global = {u.id for u in units_global}
    assert unit_global.id in returned_ids_global
    assert unit_other.id not in returned_ids_global

    units_other = await api.get_memory_units_by_chunks(
        [chunk_global.id, chunk_other.id], other_vault_id
    )
    returned_ids_other = {u.id for u in units_other}
    assert unit_other.id in returned_ids_other
    assert unit_global.id not in returned_ids_other
