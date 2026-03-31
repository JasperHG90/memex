"""Integration tests for audit logging on note write operations.

Audit entries are persisted via FastAPI BackgroundTasks, which the sync
TestClient runs to completion before returning from each request.  We
query the audit_logs table directly after each request to verify entries.
"""

import asyncio
import base64
import json
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient


def _ingest_note(client: TestClient, name: str | None = None) -> dict:
    """Ingest a note and return {raw_id, uuid_id}."""
    payload = {
        'name': name or f'Test Note {uuid4().hex[:8]}',
        'description': 'test',
        'content': base64.b64encode(f'Test content {uuid4()}'.encode()).decode(),
        'tags': ['test'],
    }
    resp = client.post('/api/v1/ingestions', json=payload)
    assert resp.status_code == 200, resp.text
    raw_id = resp.json()['note_id']
    # Ingestion returns hex without dashes; path params use dashed UUIDs
    uuid_id = str(UUID(raw_id)) if '-' not in raw_id else raw_id
    return {'raw_id': raw_id, 'uuid_id': uuid_id}


def _query_audit(postgres_url: str, action: str, resource_id: str) -> list[dict]:
    """Query audit_logs table via asyncpg (runs in a fresh event loop)."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _fetch():
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                'SELECT action, resource_type, resource_id, actor, '
                'session_id, details '
                'FROM audit_logs WHERE action = $1 AND resource_id = $2 '
                'ORDER BY "timestamp" DESC',
                action,
                resource_id,
            )
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get('details'), str):
                    d['details'] = json.loads(d['details'])
                result.append(d)
            return result
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_fetch())
    finally:
        loop.close()


@pytest.mark.integration
@pytest.mark.llm
def test_audit_note_create(client: TestClient, postgres_url: str):
    """Ingesting a note creates a note.create audit entry."""
    note_name = f'Audit Test {uuid4().hex[:8]}'
    ids = _ingest_note(client, name=note_name)

    # note.create stores the raw id from the ingestion result
    entries = _query_audit(postgres_url, 'note.create', ids['raw_id'])
    assert len(entries) == 1

    entry = entries[0]
    assert entry['action'] == 'note.create'
    assert entry['resource_type'] == 'note'
    assert entry['details'] is not None
    assert entry['details']['title'] == note_name
    assert entry['details']['path'] == '/api/v1/ingestions'
    assert entry['details']['method'] == 'POST'


@pytest.mark.integration
@pytest.mark.llm
def test_audit_note_delete(client: TestClient, postgres_url: str):
    """Deleting a note creates a note.delete audit entry."""
    ids = _ingest_note(client)
    resp = client.delete(f'/api/v1/notes/{ids["uuid_id"]}')
    assert resp.status_code == 200

    # note.delete stores str(note_id) which is dashed UUID
    entries = _query_audit(postgres_url, 'note.delete', ids['uuid_id'])
    assert len(entries) == 1

    entry = entries[0]
    assert entry['action'] == 'note.delete'
    assert entry['resource_type'] == 'note'
    assert entry['details'] is not None
    assert entry['details']['method'] == 'DELETE'
    assert 'ip' in entry['details']
    assert 'user_agent' in entry['details']


@pytest.mark.integration
@pytest.mark.llm
def test_audit_note_rename(client: TestClient, postgres_url: str):
    """Renaming a note creates a note.rename audit entry."""
    ids = _ingest_note(client)
    new_title = f'Renamed {uuid4().hex[:8]}'
    resp = client.patch(f'/api/v1/notes/{ids["uuid_id"]}/title', json={'new_title': new_title})
    assert resp.status_code == 200

    entries = _query_audit(postgres_url, 'note.rename', ids['uuid_id'])
    assert len(entries) == 1

    entry = entries[0]
    assert entry['details'] is not None
    assert entry['details']['new_title'] == new_title


@pytest.mark.integration
@pytest.mark.llm
def test_audit_note_update_status(client: TestClient, postgres_url: str):
    """Changing note status creates a note.update_status audit entry."""
    ids = _ingest_note(client)
    resp = client.patch(f'/api/v1/notes/{ids["uuid_id"]}/status', json={'status': 'archived'})
    assert resp.status_code == 200

    entries = _query_audit(postgres_url, 'note.update_status', ids['uuid_id'])
    assert len(entries) == 1

    entry = entries[0]
    assert entry['details'] is not None
    assert entry['details']['status'] == 'archived'
