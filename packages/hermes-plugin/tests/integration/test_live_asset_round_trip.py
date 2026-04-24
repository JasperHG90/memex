"""Integration test for the asset add → list → get round trip (AC-093).

Exercises the full end-to-end path: real Hermes loader × real Memex FastAPI
app × real Postgres, asserting byte-level round-trip through
base64 encoding on the Hermes side.

Per RFC-015 + POC-002: tools are invoked via
``initialized_provider.handle_tool_call(tool_name, args)`` — the canonical
path used by production Hermes agents and the existing integration suite.
"""

from __future__ import annotations

import asyncio
import base64
import json
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.hermes_integration


ASSET_BYTES = b'PNGFAKE'


@pytest.mark.asyncio
async def test_asset_add_list_get_round_trip(initialized_provider, live_api, live_vault: UUID):
    """End-to-end: retain a note → add asset → list asset → get bytes.

    Byte-equality after the round trip proves the full stack preserves
    binary content across the Hermes base64 ↔ bytes boundary.
    """
    # 1. Seed a note via memex_retain (matches test_retain_roundtrip_via_real_server pattern).
    marker = f'asset-rt-{uuid4().hex}'
    retain_raw = initialized_provider.handle_tool_call(
        'memex_retain',
        {
            'name': marker,
            'description': 'asset round-trip integration test',
            'content': f'Round-trip marker: {marker}.',
            'tags': ['integration', 'asset-round-trip'],
        },
    )
    retain_data = json.loads(retain_raw)
    assert 'error' not in retain_data, f'retain failed: {retain_data!r}'

    # 2. Poll until the background ingestion persists the note; capture its UUID.
    note_id: UUID | None = None
    for _ in range(40):
        await asyncio.sleep(0.5)
        notes = await live_api.list_notes(vault_ids=[live_vault], limit=50)
        for note in notes:
            title = getattr(note, 'title', None) or getattr(note, 'name', None)
            if title == marker:
                note_id = UUID(str(note.id))
                break
        if note_id is not None:
            break
    assert note_id is not None, f'retained note {marker!r} did not appear in listings'

    # 3. Push bytes through memex_add_assets (base64-encoded at the Hermes boundary).
    content_b64 = base64.b64encode(ASSET_BYTES).decode('ascii')
    add_raw = initialized_provider.handle_tool_call(
        'memex_add_assets',
        {
            'note_id': str(note_id),
            'assets': [{'filename': 'test.png', 'content_b64': content_b64}],
        },
    )
    add_data = json.loads(add_raw)
    assert add_data.get('status') == 'ok', f'add_assets failed: {add_data!r}'
    assert add_data['note_id'] == str(note_id)
    assert len(add_data['added_assets']) == 1

    # 4. List assets on the note, confirming the attachment is visible.
    list_raw = initialized_provider.handle_tool_call('memex_list_assets', {'note_id': str(note_id)})
    list_data = json.loads(list_raw)
    assert 'error' not in list_data, f'list_assets failed: {list_data!r}'
    assert len(list_data['results']) == 1, f'expected 1 asset, got {list_data!r}'
    asset = list_data['results'][0]
    assert asset['filename'] == 'test.png'
    asset_path = asset['path']

    # 5. Fetch bytes by path and assert byte-level round-trip equality.
    get_raw = initialized_provider.handle_tool_call('memex_get_resources', {'paths': [asset_path]})
    get_data = json.loads(get_raw)
    assert len(get_data['results']) == 1
    result = get_data['results'][0]
    assert 'error' not in result, f'get_resources error: {result!r}'
    assert base64.b64decode(result['content_b64']) == ASSET_BYTES
    assert result['size_bytes'] == len(ASSET_BYTES)
