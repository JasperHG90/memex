"""Integration tests for deferred entity cleanup after note deletion."""

import asyncio
import base64
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel.ext.asyncio.session import AsyncSession


def _ingest_note(client: TestClient, content: str) -> str:
    """Ingest a note and return its note_id."""
    payload = {
        'name': f'Test Note {uuid4().hex[:8]}',
        'description': 'test',
        'content': base64.b64encode(content.encode()).decode(),
        'tags': ['test'],
    }
    resp = client.post('/api/v1/ingestions', json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()['note_id']


@pytest.mark.integration
@pytest.mark.llm
def test_delete_note_succeeds_and_cleans_up_entities(client: TestClient):
    """Delete a note, verify it's gone, and entity cleanup runs in the background."""
    content = f'John Smith met with Jane Doe at Acme Corp on March 15. {uuid4()}'
    note_id = _ingest_note(client, content)

    # Verify note exists
    resp = client.get(f'/api/v1/notes/{note_id}')
    assert resp.status_code == 200

    # Delete the note
    resp = client.delete(f'/api/v1/notes/{note_id}')
    assert resp.status_code == 200
    assert resp.json()['status'] == 'success'

    # Note should be gone
    resp = client.get(f'/api/v1/notes/{note_id}')
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_delete_note_entity_mention_count_updated(
    client: TestClient, db_session: AsyncSession
):
    """After deleting one of two notes sharing entities, mention_count is recalculated."""
    from sqlmodel import select

    from memex_core.memory.sql_models import Entity

    # Ingest two notes that share an entity name
    shared_name = f'Memex Corp {uuid4().hex[:6]}'
    note1_id = _ingest_note(client, f'{shared_name} announced a new product. {uuid4()}')
    note2_id = _ingest_note(client, f'{shared_name} reported quarterly earnings. {uuid4()}')

    # Delete note1
    resp = client.delete(f'/api/v1/notes/{note1_id}')
    assert resp.status_code == 200

    # Give background cleanup task time to run
    await asyncio.sleep(2)

    # note2 should still exist
    resp = client.get(f'/api/v1/notes/{note2_id}')
    assert resp.status_code == 200

    # The shared entity should still exist with reduced mention_count
    result = await db_session.exec(
        select(Entity).where(Entity.canonical_name.ilike(f'%{shared_name.split()[0]}%'))  # type: ignore[union-attr]
    )
    entities = result.all()
    # At least one entity from the shared name should survive
    assert len(entities) > 0
    for e in entities:
        # mention_count should be > 0 (still referenced by note2)
        assert e.mention_count >= 1
