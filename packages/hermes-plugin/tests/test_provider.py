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
    """Subset assertion: each landed stream appends its own tool names.
    Stream 6 will tighten back to strict equality with the full 27-tool set
    once all streams have merged (AC-086).
    """
    provider, *_ = provider_with_stubbed_api
    schemas = provider.get_tool_schemas()
    names = {s['name'] for s in schemas}
    expected_minimum = {
        # Stream 1 (vault-scoped)
        'memex_recall',
        'memex_retrieve_notes',
        'memex_survey',
        'memex_retain',
        'memex_list_entities',
        'memex_get_entity_mentions',
        'memex_get_entity_cooccurrences',
        # Stream 2 (read/discovery)
        'memex_list_vaults',
        'memex_get_vault_summary',
        'memex_find_note',
        'memex_read_note',
        'memex_get_page_indices',
        'memex_get_nodes',
        'memex_get_notes_metadata',
        'memex_list_notes',
        'memex_recent_notes',
        'memex_search_user_notes',
        # Stream 3 (entities/memory/lineage)
        'memex_get_entities',
        'memex_get_memory_units',
        'memex_get_memory_links',
        'memex_get_lineage',
    }
    assert expected_minimum.issubset(names)


# Regression for v0.1.13 bug:
# Hermes calls ``get_tool_schemas()`` at provider *registration* time, before
# ``initialize()`` has run. v0.1.13 gated the schemas on ``self._config is
# None`` and returned ``[]`` there — resulting in Hermes registering 0 memex
# tools, and every subsequent model call failing with "Unknown tool".
#
# These tests cover the pre-init path explicitly.
class TestGetToolSchemasBeforeInitialize:
    def test_returns_all_seven_schemas_pre_init(self):
        """The v0.1.13 bug was returning []; we now return the full set
        pre-init. Name retains 'seven' for historical continuity; assertion
        uses ``issubset`` so streams 2-5 don't trip it. Stream 6 will tighten
        back to strict equality with all 27 names.
        """
        p = MemexMemoryProvider()
        # NOTE: no initialize() call.
        schemas = p.get_tool_schemas()
        names = {s['name'] for s in schemas}
        expected_minimum = {
            # Stream 1 (vault-scoped)
            'memex_recall',
            'memex_retrieve_notes',
            'memex_survey',
            'memex_retain',
            'memex_list_entities',
            'memex_get_entity_mentions',
            'memex_get_entity_cooccurrences',
            # Stream 2 (read/discovery)
            'memex_list_vaults',
            'memex_get_vault_summary',
            'memex_find_note',
            'memex_read_note',
            'memex_get_page_indices',
            'memex_get_nodes',
            'memex_get_notes_metadata',
            'memex_list_notes',
            'memex_recent_notes',
            'memex_search_user_notes',
            # Stream 3 (entities/memory/lineage)
            'memex_get_entities',
            'memex_get_memory_units',
            'memex_get_memory_links',
            'memex_get_lineage',
        }
        assert expected_minimum.issubset(names)

    def test_each_schema_is_well_formed(self):
        p = MemexMemoryProvider()
        for schema in p.get_tool_schemas():
            assert 'name' in schema
            assert 'description' in schema
            assert schema['parameters']['type'] == 'object'

    def test_ever_only_empty_when_explicit_context_mode(self, tmp_path: Path, monkeypatch):
        """A fresh provider with no config always exposes tools. Only an
        initialized provider whose config explicitly says ``context`` hides them.
        Uses ``>= 7`` so later streams appending tools don't break this guard.
        """
        # Pre-init: at least Stream 1's 7 tools (later streams add more).
        p = MemexMemoryProvider()
        assert len(p.get_tool_schemas()) >= 7

        # After init in context mode: empty.
        monkeypatch.setenv('HERMES_HOME', str(tmp_path))
        monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
        monkeypatch.setenv('MEMEX_HERMES_MODE', 'context')

        fake_api = Mock()
        fake_api.kv_get = AsyncMock(return_value=None)
        fake_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        fake_api.get_session_briefing = AsyncMock(return_value='')

        with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
            p2 = MemexMemoryProvider()
            p2.initialize('s', hermes_home=str(tmp_path), platform='cli')
            try:
                assert p2.get_tool_schemas() == []
            finally:
                p2.shutdown()


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


# ---------------------------------------------------------------------------
# Session-note title formatting
# ---------------------------------------------------------------------------


class TestSessionTitle:
    """The title was hardcoded 'Hermes session' in v0.1.12 — every note
    looked the same. Now it's templated and includes per-session context."""

    def _provider(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        agent_identity: str = 'coder',
        platform: str = 'cli',
        template: str | None = None,
    ):
        monkeypatch.setenv('HERMES_HOME', str(tmp_path))
        monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
        monkeypatch.setenv('MEMEX_VAULT', 'v')

        if template is not None:
            import json

            cfg_dir = tmp_path / 'memex'
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / 'config.json').write_text(
                json.dumps({'retain': {'session_title_template': template}})
            )

        fake_api = Mock()
        fake_api.kv_get = AsyncMock(return_value=None)
        fake_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        fake_api.get_session_briefing = AsyncMock(return_value='')
        fake_api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(uuid4())))
        fake_api.kv_put = AsyncMock()

        with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
            p = MemexMemoryProvider()
            p.initialize(
                'session-12345678',
                hermes_home=str(tmp_path),
                platform=platform,
                agent_identity=agent_identity,
            )
        return p, fake_api

    def test_default_template_includes_agent_platform_date(self, tmp_path, monkeypatch):
        p, _ = self._provider(tmp_path, monkeypatch)
        title = p._format_session_title()
        try:
            assert 'coder' in title
            assert 'cli' in title
            assert 'Hermes session' in title
            # ISO-ish date prefix.
            import re

            assert re.search(r'\d{4}-\d{2}-\d{2}', title)
        finally:
            p.shutdown()

    def test_user_template_with_session_id_short(self, tmp_path, monkeypatch):
        p, _ = self._provider(
            tmp_path,
            monkeypatch,
            template='S [{agent_identity}] {session_id_short}',
        )
        title = p._format_session_title()
        try:
            # session_id_short = first 8 chars of 'session-12345678' = 'session-'
            assert title == 'S [coder] session-'
        finally:
            p.shutdown()

    def test_template_unknown_key_falls_back_gracefully(self, tmp_path, monkeypatch):
        p, _ = self._provider(tmp_path, monkeypatch, template='Bad {nonexistent}')
        title = p._format_session_title()
        try:
            # Falls back to a default (still useful, won't crash).
            assert 'Hermes session' in title
        finally:
            p.shutdown()

    def test_missing_agent_identity_renders_as_agent(self, tmp_path, monkeypatch):
        p, _ = self._provider(
            tmp_path, monkeypatch, agent_identity='', template='[{agent_identity}]'
        )
        title = p._format_session_title()
        try:
            assert title == '[agent]'
        finally:
            p.shutdown()

    def test_on_session_end_uses_formatted_title(self, tmp_path, monkeypatch):
        p, api = self._provider(tmp_path, monkeypatch)
        p.sync_turn('hi', 'hello')
        p.on_session_end([])
        try:
            api.ingest.assert_awaited()
            dto = api.ingest.call_args.args[0]
            # Title is no longer the hardcoded 'Hermes session'.
            assert dto.name != 'Hermes session'
            assert 'coder' in dto.name and 'cli' in dto.name
        finally:
            p.shutdown()

    def test_pre_compress_marks_fragment_in_title(self, tmp_path, monkeypatch):
        p, api = self._provider(tmp_path, monkeypatch)
        p.on_pre_compress([{'role': 'user', 'content': 'bye'}])
        try:
            api.ingest.assert_awaited()
            dto = api.ingest.call_args.args[0]
            assert 'pre-compress fragment' in dto.name
        finally:
            p.shutdown()
