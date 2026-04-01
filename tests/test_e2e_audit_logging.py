"""E2E tests for two-layer audit logging.

Layer 1 (HTTP access log): middleware logs every request as http.request.
Layer 2 (domain events): service layer emits structured events for mutations.

Audit entries are persisted via asyncio.create_task (fire-and-forget).
The sync TestClient runs background tasks to completion before returning.
We query the audit_logs table directly after each request to verify entries.
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
    uuid_id = str(UUID(raw_id)) if '-' not in raw_id else raw_id
    return {'raw_id': raw_id, 'uuid_id': uuid_id}


def _query_audit(postgres_url: str, action: str, resource_id: str | None = None) -> list[dict]:
    """Query audit_logs table via asyncpg."""
    dsn = postgres_url.replace('postgresql+asyncpg://', 'postgresql://')

    async def _fetch():
        conn = await asyncpg.connect(dsn)
        try:
            if resource_id:
                rows = await conn.fetch(
                    'SELECT action, resource_type, resource_id, actor, '
                    'session_id, details '
                    'FROM audit_logs WHERE action = $1 AND resource_id = $2 '
                    'ORDER BY "timestamp" DESC',
                    action,
                    resource_id,
                )
            else:
                rows = await conn.fetch(
                    'SELECT action, resource_type, resource_id, actor, '
                    'session_id, details '
                    'FROM audit_logs WHERE action = $1 '
                    'ORDER BY "timestamp" DESC',
                    action,
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
def test_audit_note_ingested(client: TestClient, postgres_url: str):
    """Ingesting a note produces a note.ingested domain event."""
    note_name = f'Audit Test {uuid4().hex[:8]}'
    ids = _ingest_note(client, name=note_name)

    entries = _query_audit(postgres_url, 'note.ingested', ids['raw_id'])
    assert len(entries) == 1

    entry = entries[0]
    assert entry['action'] == 'note.ingested'
    assert entry['resource_type'] == 'note'
    assert entry['details'] is not None
    assert entry['details']['title'] == note_name


@pytest.mark.integration
@pytest.mark.llm
def test_audit_http_request_logged(client: TestClient, postgres_url: str):
    """Every API request produces an http.request access log entry."""
    _ingest_note(client)

    entries = _query_audit(postgres_url, 'http.request')
    # At least 1 http.request entry for the ingestion POST
    assert len(entries) >= 1

    # Find the ingestion entry
    ingest_entries = [
        e for e in entries if e['details'] and e['details'].get('path') == '/api/v1/ingestions'
    ]
    assert len(ingest_entries) >= 1
    entry = ingest_entries[0]
    assert entry['details']['method'] == 'POST'
    assert entry['details']['status'] == 200
    assert 'latency_ms' in entry['details']


@pytest.mark.integration
@pytest.mark.llm
def test_audit_note_deleted(client: TestClient, postgres_url: str):
    """Deleting a note produces a note.deleted domain event."""
    ids = _ingest_note(client)
    resp = client.delete(f'/api/v1/notes/{ids["uuid_id"]}')
    assert resp.status_code == 200

    entries = _query_audit(postgres_url, 'note.deleted', ids['uuid_id'])
    assert len(entries) == 1

    entry = entries[0]
    assert entry['action'] == 'note.deleted'
    assert entry['resource_type'] == 'note'


@pytest.mark.integration
@pytest.mark.llm
def test_audit_note_renamed(client: TestClient, postgres_url: str):
    """Renaming a note produces a note.renamed domain event."""
    ids = _ingest_note(client)
    new_title = f'Renamed {uuid4().hex[:8]}'
    resp = client.patch(f'/api/v1/notes/{ids["uuid_id"]}/title', json={'new_title': new_title})
    assert resp.status_code == 200

    entries = _query_audit(postgres_url, 'note.renamed', ids['uuid_id'])
    assert len(entries) == 1

    entry = entries[0]
    assert entry['details'] is not None
    assert entry['details']['new_title'] == new_title


@pytest.mark.integration
@pytest.mark.llm
def test_audit_note_status_changed(client: TestClient, postgres_url: str):
    """Changing note status produces a note.status_changed domain event."""
    ids = _ingest_note(client)
    resp = client.patch(f'/api/v1/notes/{ids["uuid_id"]}/status', json={'status': 'archived'})
    assert resp.status_code == 200

    entries = _query_audit(postgres_url, 'note.status_changed', ids['uuid_id'])
    assert len(entries) == 1

    entry = entries[0]
    assert entry['details'] is not None
    assert entry['details']['status'] == 'archived'
