"""Smoke tests for MemexMemoryProvider lifecycle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from memex_hermes_plugin.memex.provider import MemexMemoryProvider


@pytest.fixture
def provider_with_stubbed_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'test-vault')

    fake_api = Mock()
    vault_uuid = uuid4()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(return_value=vault_uuid)
    fake_api.get_session_briefing = AsyncMock(return_value='# Briefing')
    fake_api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(uuid4())))
    fake_api.kv_put = AsyncMock()

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        provider = MemexMemoryProvider()
        provider.initialize('session-abc', hermes_home=str(tmp_path), platform='cli')
        yield provider, fake_api, vault_uuid
    provider.shutdown()


def test_name_is_memex():
    p = MemexMemoryProvider()
    assert p.name == 'memex'


def test_is_available_with_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://x')
    assert MemexMemoryProvider().is_available() is True


def test_initialize_fetches_briefing_and_sets_vault(provider_with_stubbed_api):
    provider, api, vault_uuid = provider_with_stubbed_api
    assert provider._vault_name == 'test-vault'
    assert provider._vault_id == vault_uuid
    # Session note key format.
    assert provider._session_note_key.startswith('hermes:session:')
    # system_prompt_block includes the briefing text.
    block = provider.system_prompt_block()
    assert 'Memex Memory' in block
    assert '# Briefing' in block


def test_get_tool_schemas_respects_memory_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_HERMES_MODE', 'context')

    fake_api = Mock()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    fake_api.get_session_briefing = AsyncMock(return_value='')

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        provider = MemexMemoryProvider()
        provider.initialize('s', hermes_home=str(tmp_path), platform='cli')
        try:
            assert provider.get_tool_schemas() == []
        finally:
            provider.shutdown()


def test_get_tool_schemas_in_hybrid_mode(provider_with_stubbed_api):
    provider, *_ = provider_with_stubbed_api
    schemas = provider.get_tool_schemas()
    names = {s['name'] for s in schemas}
    assert names == {
        'memex_recall',
        'memex_retrieve_notes',
        'memex_survey',
        'memex_retain',
        'memex_list_entities',
        'memex_get_entity_mentions',
        'memex_get_entity_cooccurrences',
    }


def test_sync_turn_buffers(provider_with_stubbed_api):
    provider, *_ = provider_with_stubbed_api
    provider.sync_turn('hi', 'hello', session_id='s')
    assert len(provider._turn_buffer) == 1
    assert provider._turn_buffer[0]['user'] == 'hi'


def test_on_session_end_ingests_transcript(provider_with_stubbed_api):
    provider, api, _ = provider_with_stubbed_api
    provider.sync_turn('ping', 'pong', session_id='s')
    provider.on_session_end([])
    api.ingest.assert_awaited()
    dto = api.ingest.call_args.args[0]
    assert dto.note_key == provider._session_note_key
    # Transcript should contain the buffered content.
    import base64

    body = base64.b64decode(dto.content).decode('utf-8')
    assert 'ping' in body
    assert 'pong' in body


def test_on_memory_write_mirrors_to_kv(provider_with_stubbed_api):
    provider, api, _ = provider_with_stubbed_api
    provider.on_memory_write('add', 'user', 'Prefers Rust')
    api.kv_put.assert_awaited()
    kwargs = api.kv_put.call_args.kwargs
    assert kwargs['value'] == 'Prefers Rust'
    # Key must start with a Memex VALID_NAMESPACES prefix (app/user/project/global).
    from memex_core.services.kv import VALID_NAMESPACES

    prefix = kwargs['key'].split(':', 1)[0]
    assert prefix in VALID_NAMESPACES, (
        f'KV key {kwargs["key"]!r} prefix {prefix!r} not in VALID_NAMESPACES={VALID_NAMESPACES}'
    )
    assert kwargs['key'].startswith('app:hermes:user:')


def test_on_memory_write_remove_is_noop(provider_with_stubbed_api):
    provider, api, _ = provider_with_stubbed_api
    api.kv_put.reset_mock()
    provider.on_memory_write('remove', 'user', 'Prefers Rust')
    api.kv_put.assert_not_called()


def test_shutdown_is_safe_to_call_twice(provider_with_stubbed_api):
    provider, *_ = provider_with_stubbed_api
    provider.shutdown()
    # Second call is a no-op.
    provider.shutdown()
