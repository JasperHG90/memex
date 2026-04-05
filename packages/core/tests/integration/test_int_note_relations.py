"""Integration tests for note_relations.py query functions and related endpoints.

Requires Docker (testcontainers with pgvector).
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from memex_common.config import GLOBAL_VAULT_ID

import pytest
from sqlalchemy import text

from memex_common.schemas import MemoryLinkDTO
from memex_core.memory.retrieval.note_relations import (
    compute_related_notes,
    fetch_memory_links,
    fetch_memory_links_for_notes,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_note(session, note_id: UUID, vault_id: UUID, title: str = 'Test Note'):
    await session.execute(
        text("""
            INSERT INTO notes (id, vault_id, content_hash, title)
            VALUES (:id, :vid, :hash, :title)
            ON CONFLICT (id) DO NOTHING
        """),
        {'id': str(note_id), 'vid': str(vault_id), 'hash': f'hash-{note_id}', 'title': title},
    )


async def _seed_unit(
    session, unit_id: UUID, note_id: UUID, vault_id: UUID, text_content: str = 'Fact'
):
    await session.execute(
        text("""
            INSERT INTO memory_units (id, note_id, vault_id, text, fact_type, embedding, event_date)
            VALUES (:id, :nid, :vid, :text, 'world', :emb, :ed)
            ON CONFLICT (id) DO NOTHING
        """),
        {
            'id': str(unit_id),
            'nid': str(note_id),
            'vid': str(vault_id),
            'text': text_content,
            'emb': str([0.1] * 384),
            'ed': datetime.now(timezone.utc),
        },
    )


async def _seed_link(
    session,
    from_id: UUID,
    to_id: UUID,
    vault_id: UUID,
    link_type: str = 'semantic',
    weight: float = 0.8,
):
    await session.execute(
        text("""
            INSERT INTO memory_links (from_unit_id, to_unit_id, vault_id, link_type, weight)
            VALUES (:from_id, :to_id, :vid, :lt, :w)
            ON CONFLICT DO NOTHING
        """),
        {
            'from_id': str(from_id),
            'to_id': str(to_id),
            'vid': str(vault_id),
            'lt': link_type,
            'w': weight,
        },
    )


async def _seed_entity(session, entity_id: UUID, vault_id: UUID, name: str, mention_count: int = 1):
    await session.execute(
        text("""
            INSERT INTO entities (id, canonical_name, mention_count)
            VALUES (:id, :name, :mc)
            ON CONFLICT (id) DO NOTHING
        """),
        {'id': str(entity_id), 'name': name, 'mc': mention_count},
    )


async def _seed_unit_entity(session, unit_id: UUID, entity_id: UUID, vault_id: UUID):
    await session.execute(
        text("""
            INSERT INTO unit_entities (unit_id, entity_id)
            VALUES (:uid, :eid)
            ON CONFLICT DO NOTHING
        """),
        {'uid': str(unit_id), 'eid': str(entity_id)},
    )


# ---------------------------------------------------------------------------
# 14. fetch_memory_links — basic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_memory_links_basic(session):
    """Seed 2 units with a MemoryLink between them. Verify correct DTO returned."""
    vid = GLOBAL_VAULT_ID  # global vault
    nid_a, nid_b = uuid4(), uuid4()
    uid_a, uid_b = uuid4(), uuid4()

    await _seed_note(session, nid_a, vid, 'Note A')
    await _seed_note(session, nid_b, vid, 'Note B')
    await _seed_unit(session, uid_a, nid_a, vid, f'Fact A {uuid4()}')
    await _seed_unit(session, uid_b, nid_b, vid, f'Fact B {uuid4()}')
    await _seed_link(session, uid_a, uid_b, vid, 'semantic', 0.75)
    await session.commit()

    result = await fetch_memory_links(session, [uid_a])

    assert uid_a in result
    links = result[uid_a]
    assert len(links) >= 1
    link = links[0]
    assert isinstance(link, MemoryLinkDTO)
    assert link.unit_id == uid_b
    assert link.note_title == 'Note B'
    assert link.relation == 'semantic'
    assert link.weight == 0.75


# ---------------------------------------------------------------------------
# 15. fetch_memory_links — bidirectional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_memory_links_bidirectional(session):
    """Query from either side of a link. Both directions return results."""
    vid = GLOBAL_VAULT_ID
    nid_a, nid_b = uuid4(), uuid4()
    uid_a, uid_b = uuid4(), uuid4()

    await _seed_note(session, nid_a, vid, 'Note A')
    await _seed_note(session, nid_b, vid, 'Note B')
    await _seed_unit(session, uid_a, nid_a, vid, f'Fact A {uuid4()}')
    await _seed_unit(session, uid_b, nid_b, vid, f'Fact B {uuid4()}')
    await _seed_link(session, uid_a, uid_b, vid, 'temporal', 0.9)
    await session.commit()

    # Query from uid_a side
    result_a = await fetch_memory_links(session, [uid_a])
    assert uid_a in result_a

    # Query from uid_b side
    result_b = await fetch_memory_links(session, [uid_b])
    assert uid_b in result_b


# ---------------------------------------------------------------------------
# 15b. fetch_memory_links — both sides in input set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_memory_links_both_sides_in_set(session):
    """When both from_unit_id and to_unit_id are in the input set, both get link entries."""
    vid = GLOBAL_VAULT_ID
    nid_a, nid_b = uuid4(), uuid4()
    uid_a, uid_b = uuid4(), uuid4()

    await _seed_note(session, nid_a, vid, 'Note A')
    await _seed_note(session, nid_b, vid, 'Note B')
    await _seed_unit(session, uid_a, nid_a, vid, f'Fact A {uuid4()}')
    await _seed_unit(session, uid_b, nid_b, vid, f'Fact B {uuid4()}')
    await _seed_link(session, uid_a, uid_b, vid, 'semantic', 0.85)
    await session.commit()

    # Query with BOTH unit IDs in the input set
    result = await fetch_memory_links(session, [uid_a, uid_b])

    # uid_a should have a link pointing to uid_b
    assert uid_a in result
    a_links = result[uid_a]
    assert any(lnk.unit_id == uid_b for lnk in a_links)
    assert any(lnk.note_title == 'Note B' for lnk in a_links)

    # uid_b should have a link pointing to uid_a
    assert uid_b in result
    b_links = result[uid_b]
    assert any(lnk.unit_id == uid_a for lnk in b_links)
    assert any(lnk.note_title == 'Note A' for lnk in b_links)


# ---------------------------------------------------------------------------
# 16. fetch_memory_links — no links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_memory_links_no_links(session):
    """Query for units with no links. Verify empty dict."""
    vid = GLOBAL_VAULT_ID
    uid = uuid4()
    nid = uuid4()

    await _seed_note(session, nid, vid, 'Lonely Note')
    await _seed_unit(session, uid, nid, vid, f'Lonely fact {uuid4()}')
    await session.commit()

    result = await fetch_memory_links(session, [uid])
    assert result == {}


# ---------------------------------------------------------------------------
# 17. fetch_memory_links_for_notes — aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_memory_links_for_notes_aggregation(session):
    """Seed note with 2 units, each with links. Verify note-level aggregation and dedup."""
    vid = GLOBAL_VAULT_ID
    nid_src = uuid4()
    nid_tgt = uuid4()
    uid_a, uid_b = uuid4(), uuid4()
    uid_tgt = uuid4()

    await _seed_note(session, nid_src, vid, 'Source Note')
    await _seed_note(session, nid_tgt, vid, 'Target Note')
    await _seed_unit(session, uid_a, nid_src, vid, f'Fact A {uuid4()}')
    await _seed_unit(session, uid_b, nid_src, vid, f'Fact B {uuid4()}')
    await _seed_unit(session, uid_tgt, nid_tgt, vid, f'Target fact {uuid4()}')

    # Both units link to same target with same relation — should dedup (keep highest weight)
    await _seed_link(session, uid_a, uid_tgt, vid, 'semantic', 0.6)
    await _seed_link(session, uid_b, uid_tgt, vid, 'semantic', 0.9)
    await session.commit()

    result = await fetch_memory_links_for_notes(session, [nid_src])

    assert nid_src in result
    links = result[nid_src]
    # Should be deduplicated: same target note + same relation = keep highest weight
    semantic_links = [lnk for lnk in links if lnk.relation == 'semantic']
    assert len(semantic_links) == 1
    assert semantic_links[0].weight == 0.9


# ---------------------------------------------------------------------------
# 18. compute_related_notes — shared entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_related_notes_shared_entities(session):
    """3 notes: A and B share entity X, A and C share entity Y."""
    vid = GLOBAL_VAULT_ID
    nid_a, nid_b, nid_c = uuid4(), uuid4(), uuid4()
    uid_a, uid_b, uid_c = uuid4(), uuid4(), uuid4()
    eid_x, eid_y = uuid4(), uuid4()

    await _seed_note(session, nid_a, vid, 'Note A')
    await _seed_note(session, nid_b, vid, 'Note B')
    await _seed_note(session, nid_c, vid, 'Note C')
    await _seed_unit(session, uid_a, nid_a, vid, f'Fact A {uuid4()}')
    await _seed_unit(session, uid_b, nid_b, vid, f'Fact B {uuid4()}')
    await _seed_unit(session, uid_c, nid_c, vid, f'Fact C {uuid4()}')

    await _seed_entity(session, eid_x, vid, 'EntityX', mention_count=3)
    await _seed_entity(session, eid_y, vid, 'EntityY', mention_count=2)

    # A shares X with B, and Y with C
    await _seed_unit_entity(session, uid_a, eid_x, vid)
    await _seed_unit_entity(session, uid_a, eid_y, vid)
    await _seed_unit_entity(session, uid_b, eid_x, vid)
    await _seed_unit_entity(session, uid_c, eid_y, vid)
    await session.commit()

    # Default: shared_entities omitted (max_shared_entities=0)
    result_default = await compute_related_notes(session, [nid_a])
    assert nid_a in result_default
    related_default = result_default[nid_a]
    related_ids = {r.note_id for r in related_default}
    assert nid_b in related_ids
    assert nid_c in related_ids
    for r in related_default:
        assert r.shared_entities == []
        assert r.strength > 0

    # With shared_entities enabled
    result = await compute_related_notes(session, [nid_a], max_shared_entities=3)
    related = result[nid_a]
    for r in related:
        assert len(r.shared_entities) >= 1
        assert r.strength > 0


# ---------------------------------------------------------------------------
# 19. compute_related_notes — entity fan-out cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_related_notes_fanout_cap(session):
    """Entity with mention_count > 50 is excluded from relation computation."""
    vid = GLOBAL_VAULT_ID
    nid_a, nid_b = uuid4(), uuid4()
    uid_a, uid_b = uuid4(), uuid4()
    eid_generic = uuid4()

    await _seed_note(session, nid_a, vid, 'Note A')
    await _seed_note(session, nid_b, vid, 'Note B')
    await _seed_unit(session, uid_a, nid_a, vid, f'Fact A {uuid4()}')
    await _seed_unit(session, uid_b, nid_b, vid, f'Fact B {uuid4()}')

    # Generic entity with high mention count
    await _seed_entity(session, eid_generic, vid, 'Software', mention_count=100)
    await _seed_unit_entity(session, uid_a, eid_generic, vid)
    await _seed_unit_entity(session, uid_b, eid_generic, vid)
    await session.commit()

    result = await compute_related_notes(session, [nid_a])

    # Should be empty — only shared entity exceeds fanout cap
    assert nid_a not in result or result[nid_a] == []


# ---------------------------------------------------------------------------
# 20. compute_related_notes — no relations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_related_notes_no_relations(session):
    """Note with unique entities. Verify empty result."""
    vid = GLOBAL_VAULT_ID
    nid = uuid4()
    uid = uuid4()
    eid = uuid4()

    await _seed_note(session, nid, vid, 'Unique Note')
    await _seed_unit(session, uid, nid, vid, f'Unique fact {uuid4()}')
    await _seed_entity(session, eid, vid, f'UniqueEntity-{uuid4()}', mention_count=1)
    await _seed_unit_entity(session, uid, eid, vid)
    await session.commit()

    result = await compute_related_notes(session, [nid])

    # No other notes share this entity, so empty
    assert result == {}


# ---------------------------------------------------------------------------
# 21. compute_related_notes — top 5 limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_related_notes_top5_limit(session):
    """Seed note related to 8 others. Verify only top 5 returned."""
    vid = GLOBAL_VAULT_ID
    nid_src = uuid4()
    uid_src = uuid4()
    eid_shared = uuid4()

    await _seed_note(session, nid_src, vid, 'Source')
    await _seed_unit(session, uid_src, nid_src, vid, f'Source fact {uuid4()}')
    await _seed_entity(session, eid_shared, vid, 'SharedEntity', mention_count=5)
    await _seed_unit_entity(session, uid_src, eid_shared, vid)

    # Create 8 candidate notes sharing the entity
    for i in range(8):
        cnid = uuid4()
        cuid = uuid4()
        await _seed_note(session, cnid, vid, f'Candidate {i}')
        await _seed_unit(session, cuid, cnid, vid, f'Candidate fact {uuid4()}')
        await _seed_unit_entity(session, cuid, eid_shared, vid)

    await session.commit()

    result = await compute_related_notes(session, [nid_src])

    assert nid_src in result
    assert len(result[nid_src]) <= 5


# ---------------------------------------------------------------------------
# 23. MemexAPI.get_related_notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_get_related_notes(session, api):
    """MemexAPI.get_related_notes returns related notes via shared entities."""
    vid = GLOBAL_VAULT_ID
    nid_a, nid_b = uuid4(), uuid4()
    uid_a, uid_b = uuid4(), uuid4()
    eid = uuid4()

    await _seed_note(session, nid_a, vid, 'API Note A')
    await _seed_note(session, nid_b, vid, 'API Note B')
    await _seed_unit(session, uid_a, nid_a, vid, f'API Fact A {uuid4()}')
    await _seed_unit(session, uid_b, nid_b, vid, f'API Fact B {uuid4()}')
    await _seed_entity(session, eid, vid, 'SharedAPIEntity', mention_count=3)
    await _seed_unit_entity(session, uid_a, eid, vid)
    await _seed_unit_entity(session, uid_b, eid, vid)
    await session.commit()

    result = await api.get_related_notes([nid_a])

    assert nid_a in result
    related_ids = {r.note_id for r in result[nid_a]}
    assert nid_b in related_ids


# ---------------------------------------------------------------------------
# 24. POST /api/v1/notes/related endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_related_notes_endpoint(session, api):
    """POST /api/v1/notes/related returns related notes."""
    from httpx import AsyncClient, ASGITransport
    from memex_core.server import app

    vid = GLOBAL_VAULT_ID
    nid_a, nid_b = uuid4(), uuid4()
    uid_a, uid_b = uuid4(), uuid4()
    eid = uuid4()

    await _seed_note(session, nid_a, vid, 'Endpoint Note A')
    await _seed_note(session, nid_b, vid, 'Endpoint Note B')
    await _seed_unit(session, uid_a, nid_a, vid, f'Endpoint Fact A {uuid4()}')
    await _seed_unit(session, uid_b, nid_b, vid, f'Endpoint Fact B {uuid4()}')
    await _seed_entity(session, eid, vid, 'SharedEndpointEntity', mention_count=2)
    await _seed_unit_entity(session, uid_a, eid, vid)
    await _seed_unit_entity(session, uid_b, eid, vid)
    await session.commit()

    await api.initialize()
    app.state.api = api

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        resp = await client.post(
            '/api/v1/notes/related',
            json={'note_ids': [str(nid_a)]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert str(nid_a) in data
    related = data[str(nid_a)]
    assert len(related) >= 1
    assert any(r['note_id'] == str(nid_b) for r in related)
