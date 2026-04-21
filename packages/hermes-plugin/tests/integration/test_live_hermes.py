"""Live end-to-end: real Hermes loader × real Memex FastAPI app × real Postgres.

The Memex server runs under uvicorn in a background thread, backed by a
testcontainers Postgres. The plugin talks over real HTTP and goes through
Hermes' own ``plugins.memory`` discovery + registration machinery.

No API mocks. Only LLM-bound behaviour (DSPy extraction, survey decomposition,
reranker) is avoided; the rest of the round-trip is real.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

pytestmark = pytest.mark.hermes_integration


def test_discovery_lists_memex(installed_plugin: Path):
    from plugins.memory import discover_memory_providers  # type: ignore[import-not-found]

    providers = {name: (desc, available) for name, desc, available in discover_memory_providers()}
    assert 'memex' in providers
    assert providers['memex'][1] is True  # available
    assert 'Memex' in providers['memex'][0]


def test_loaded_provider_subclasses_memory_provider_abc(loaded_provider):
    from agent.memory_provider import MemoryProvider  # type: ignore[import-not-found]

    assert isinstance(loaded_provider, MemoryProvider)
    assert loaded_provider.name == 'memex'


def test_initialize_resolves_vault_and_fetches_real_briefing(
    initialized_provider, live_vault: UUID
):
    assert initialized_provider._vault_id == live_vault
    assert initialized_provider._session_note_key.startswith('hermes:session:')
    block = initialized_provider.system_prompt_block()
    # The real briefing endpoint returns markdown with vault listings.
    assert '## Memex Memory' in block
    assert initialized_provider._session_note_key in block


def test_get_tool_schemas_exposes_7_tools(initialized_provider):
    names = {s['name'] for s in initialized_provider.get_tool_schemas()}
    assert names == {
        'memex_recall',
        'memex_retrieve_notes',
        'memex_survey',
        'memex_retain',
        'memex_list_entities',
        'memex_get_entity_mentions',
        'memex_get_entity_cooccurrences',
    }


@pytest.mark.asyncio
async def test_retain_roundtrip_via_real_server(initialized_provider, live_api, live_vault: UUID):
    """memex_retain → note lands in Postgres; verified by listing server-side."""
    marker = f'integration-marker-{UUID(int=0).hex}'
    result = initialized_provider.handle_tool_call(
        'memex_retain',
        {
            'name': 'integration-note',
            'description': 'integration test capture',
            'content': f'The marker is {marker}. The answer is 42.',
            'tags': ['integration'],
        },
    )
    data = json.loads(result)
    assert 'error' not in data, f'retain failed: {data}'

    # Wait a bit for the background batch job to persist the note.
    import asyncio

    for _ in range(20):
        await asyncio.sleep(0.5)
        response = await live_api.client.get(
            'notes', params={'vault_id': str(live_vault), 'limit': 10}
        )
        if response.status_code == 200:
            body = response.text
            if 'integration-note' in body:
                return
    pytest.fail(
        f'retained note did not appear in /notes listing; last status={response.status_code}'
    )


def test_on_session_end_persists_transcript(initialized_provider, loaded_provider):
    initialized_provider.sync_turn('hello', 'hi there')
    initialized_provider.sync_turn('what did I say?', 'you said hello')
    initialized_provider.on_session_end([])
    # Transcript was buffered, then cleared after ingest.
    assert initialized_provider._turn_buffer == []


def test_on_memory_write_kv_roundtrip(initialized_provider, memex_server_url: str):
    """KV mirror must satisfy Memex's VALID_NAMESPACES contract end-to-end.

    Uses sync ``httpx.Client`` against the uvicorn-in-thread server so we stay
    off pytest-asyncio's loop (the plugin already spins up its own loop).
    """
    import hashlib

    import httpx

    from memex_core.services.kv import VALID_NAMESPACES

    initialized_provider.on_memory_write('add', 'user', 'Prefers Rust for systems code')

    digest = hashlib.sha256(b'Prefers Rust for systems code').hexdigest()[:12]
    expected_key = f'app:hermes:user:{digest}'
    with httpx.Client(base_url=f'{memex_server_url}/api/v1/', timeout=10.0) as client:
        response = client.get('kv/get', params={'key': expected_key})
    assert response.status_code == 200, f'KV lookup failed: {response.status_code} {response.text}'
    payload = response.json()
    assert payload['key'] == expected_key
    prefix = payload['key'].split(':', 1)[0]
    assert prefix in VALID_NAMESPACES
    assert payload['value'] == 'Prefers Rust for systems code'


def test_list_entities_on_empty_graph_returns_empty(initialized_provider):
    """No notes → no entities — should not error."""
    result = initialized_provider.handle_tool_call(
        'memex_list_entities', {'query': 'nonexistent-entity-xyz'}
    )
    data = json.loads(result)
    assert 'error' not in data
    assert data['results'] == []


def test_recall_with_no_matches_returns_empty(initialized_provider):
    result = initialized_provider.handle_tool_call(
        'memex_recall', {'query': 'zyxwvutsrq-no-match-hermes-integration'}
    )
    data = json.loads(result)
    assert 'error' not in data
    assert data['count'] == 0


def test_shutdown_is_idempotent(loaded_provider, hermes_home: Path):
    loaded_provider.initialize('shutdown-test', hermes_home=str(hermes_home), platform='cli')
    loaded_provider.shutdown()
    loaded_provider.shutdown()  # must not raise


def test_memory_mode_tools_hides_briefing_and_prefetch(
    installed_plugin,  # noqa: ARG001
    server_url_env: str,  # noqa: ARG001 — MEMEX_SERVER_URL set
    live_vault: UUID,  # noqa: ARG001 — vault exists
    vault_name: str,
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv('MEMEX_HERMES_MODE', 'tools')
    monkeypatch.setenv('MEMEX_VAULT', vault_name)
    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]

    provider = load_memory_provider('memex')
    try:
        provider.initialize('mode-tools', hermes_home=str(hermes_home), platform='cli')
        assert provider.system_prompt_block() == ''
        assert len(provider.get_tool_schemas()) == 7
    finally:
        provider.shutdown()
