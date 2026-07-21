"""Smoke tests for MemexMemoryProvider lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest

from memex_hermes_plugin.memex.provider import MemexMemoryProvider


def _write_test_config(tmp_path: Path) -> None:
    """Write a config that disables the quality gate so short test turns pass."""
    cfg_dir = tmp_path / 'memex'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / 'config.json'
    existing = json.loads(cfg.read_text()) if cfg.exists() else {}
    existing.setdefault('retain', {}).update(
        {
            'min_capture_turns': 0,
            'min_capture_chars': 0,
        }
    )
    cfg.write_text(json.dumps(existing))


def _drain(provider: MemexMemoryProvider) -> None:
    """Deterministically finish the async background drain in a test.

    Production drains are fire-and-forget (a daemon worker). Joining the
    active worker makes the post-conditions observable — the worker loops
    until the queue is empty, so one call before the assertions drains
    everything currently queued.
    """
    worker = provider._bg_drain_thread
    if worker is not None:
        worker.join(timeout=10.0)


@pytest.fixture
def provider_with_stubbed_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'test-vault')
    _write_test_config(tmp_path)

    fake_api = Mock()
    vault_uuid = uuid4()
    note_uuid = uuid4()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(return_value=vault_uuid)
    fake_api.get_session_briefing = AsyncMock(return_value='# Briefing')
    fake_api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(note_uuid)))
    fake_api.get_note = AsyncMock(return_value=SimpleNamespace(id=note_uuid))
    fake_api.head_note = AsyncMock(return_value=True)
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


def test_get_tool_schemas_in_tools_mode_returns_primary_seven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``memory_mode='tools'`` exposes only the 7-tool primary subset.

    Regression for the Tier-A 46→7 leak: when the test was first written
    (commit e3eb9be) ``ALL_SCHEMAS`` happened to contain exactly seven
    entries, so a no-op ``tools``-mode filter satisfied the integration
    test by accident. Successive Tier-A waves (F4/F5/F8/F9/F14/F20/F32 +
    append_note + Stream 2-5 schemas) grew the surface to 46, which
    silently leaked into ``tools`` mode and broke the agent contract.

    The contract ``tools`` mode encodes: briefing skipped, prefetch
    skipped, narrow tool surface — only the LLM-most-reached-for verbs.
    Locking it down here so the next schema landing has to consciously
    decide whether to promote the verb to ``TOOLS_MODE_SCHEMAS``.
    """
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_HERMES_MODE', 'tools')

    fake_api = Mock()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    fake_api.get_session_briefing = AsyncMock(return_value='')

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        provider = MemexMemoryProvider()
        provider.initialize('s', hermes_home=str(tmp_path), platform='cli')
        try:
            schemas = provider.get_tool_schemas()
            names = {s['name'] for s in schemas}
            assert names == {
                'memex_memory_search',
                'memex_note_search',
                'memex_survey',
                'memex_add_note',
                'memex_list_entities',
                'memex_get_entity_mentions',
                'memex_get_entity_cooccurrences',
            }
            assert len(schemas) == 7
        finally:
            provider.shutdown()


def test_get_tool_schemas_in_hybrid_mode(provider_with_stubbed_api):
    """Hybrid mode exposes exactly the 40 Memex tools (AC-086 + AC-008 + Tier A F4/F5/F29 + F32 diagnostics + F8 + F20)."""
    provider, *_ = provider_with_stubbed_api
    schemas = provider.get_tool_schemas()
    names = {s['name'] for s in schemas}
    expected = {
        # Stream 1 (vault-scoped)
        'memex_memory_search',
        'memex_note_search',
        'memex_survey',
        'memex_add_note',
        'memex_append_note',
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
        # Stream 4 (lifecycle/templates)
        'memex_set_note_status',
        'memex_update_user_notes',
        'memex_rename_note',
        'memex_get_template',
        'memex_list_templates',
        'memex_register_template',
        # Stream 5 (assets/KV)
        'memex_list_assets',
        'memex_get_resources',
        'memex_resize_image',
        'memex_add_assets',
        'memex_kv_put',
        'memex_kv_get',
        'memex_kv_search',
        'memex_kv_list',
        # Tier A WS-quick-wins (F4 + F5)
        'memex_memory_deprioritize',
        'memex_memory_restore',
        'memex_memory_summarize_node',
        # Tier A WS-quick-wins (F14 / F29 — record_outcome Hermes parity)
        'memex_record_outcome',
        # Tier A WS-diagnostics (F32)
        'memex_get_diagnostics_summary',
        # Tier A WS-linter (F8)
        'memex_get_lint_flags',
        'memex_lint_apply_winner',
        'memex_lint_reverse_winner',
        'memex_list_lint_actions',
        'memex_submit_lint_proposal',
        # Tier A WS-locks (F9)
        'memex_memory_reconsolidate',
        'memex_memory_consolidate',
        # Tier A WS-history (F49 — contradiction-graph timeline)
        'memex_get_unit_history',
        # Procedural plane reads (procedure / strategy) + case submission.
        # Writes (create/upsert/update/deprecate) are NOT exposed — procedures
        # are derived from cases; the agent's only procedural write is case_submit.
        'memex_procedural_get',
        'memex_procedural_get_by_identity',
        'memex_procedural_search',
        'memex_case_submit',
    }
    assert names == expected


# Regression for v0.1.13 bug:
# Hermes calls ``get_tool_schemas()`` at provider *registration* time, before
# ``initialize()`` has run. v0.1.13 gated the schemas on ``self._config is
# None`` and returned ``[]`` there — resulting in Hermes registering 0 memex
# tools, and every subsequent model call failing with "Unknown tool".
#
# These tests cover the pre-init path explicitly.
class TestGetToolSchemasBeforeInitialize:
    def test_returns_all_schemas_pre_init(self):
        """The v0.1.13 bug was returning []; we now return the full set
        pre-init. The Tier A roster (quick-wins + diagnostics + lint +
        locks + history) plus Stream 1-5 baselines totals 41 tools, and
        the assertion is strict equality.
        """
        p = MemexMemoryProvider()
        # NOTE: no initialize() call.
        schemas = p.get_tool_schemas()
        names = {s['name'] for s in schemas}
        expected = {
            # Stream 1 (vault-scoped)
            'memex_memory_search',
            'memex_note_search',
            'memex_survey',
            'memex_add_note',
            'memex_append_note',
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
            # Stream 4 (lifecycle/templates)
            'memex_set_note_status',
            'memex_update_user_notes',
            'memex_rename_note',
            'memex_get_template',
            'memex_list_templates',
            'memex_register_template',
            # Stream 5 (assets/KV)
            'memex_list_assets',
            'memex_get_resources',
            'memex_resize_image',
            'memex_add_assets',
            'memex_kv_put',
            'memex_kv_get',
            'memex_kv_search',
            'memex_kv_list',
            # Tier A WS-quick-wins (F4 + F5)
            'memex_memory_deprioritize',
            'memex_memory_restore',
            'memex_memory_summarize_node',
            # Tier A WS-quick-wins (F14 / F29 — record_outcome Hermes parity)
            'memex_record_outcome',
            # Tier A WS-diagnostics (F32)
            'memex_get_diagnostics_summary',
            # Tier A WS-linter (F8)
            'memex_get_lint_flags',
            'memex_lint_apply_winner',
            'memex_lint_reverse_winner',
            'memex_list_lint_actions',
            'memex_submit_lint_proposal',
            # Tier A WS-locks (F9)
            'memex_memory_reconsolidate',
            'memex_memory_consolidate',
            # Tier A WS-history (F49 — contradiction-graph timeline)
            'memex_get_unit_history',
            # Procedural plane reads (procedure / strategy) + case submission.
            # Writes are NOT exposed — procedures are derived from cases; the
            # agent's only procedural write is case_submit.
            'memex_procedural_get',
            'memex_procedural_get_by_identity',
            'memex_procedural_search',
            'memex_case_submit',
        }
        assert names == expected

    def test_each_schema_is_well_formed(self):
        p = MemexMemoryProvider()
        for schema in p.get_tool_schemas():
            assert 'name' in schema
            assert 'description' in schema
            assert schema['parameters']['type'] == 'object'

    def test_ever_only_empty_when_explicit_context_mode(self, tmp_path: Path, monkeypatch):
        """A fresh provider with no config always exposes tools. Only an
        initialized provider whose config explicitly says ``context`` hides them.
        """
        # Pre-init: full 53-tool set (Stream 1-5 baseline + Tier A
        # quick-wins + diagnostics + lint (5) + locks + history +
        # procedural plane reads (3) + case_submit). The 4 procedural
        # WRITE tools are not exposed (procedures are derived from cases).
        p = MemexMemoryProvider()
        assert len(p.get_tool_schemas()) == 53

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
    _drain(provider)
    api.ingest.assert_awaited()
    dto = api.ingest.call_args.args[0]
    assert dto.note_key == provider._session_note_key
    # Transcript should contain the buffered content.
    import base64

    body = base64.b64decode(dto.content).decode('utf-8')
    assert 'ping' in body
    assert 'pong' in body


# ---------------------------------------------------------------------------
# Transcript persistence — ingest→append split (Part A)
# ---------------------------------------------------------------------------
#
# Background: the prior implementation called ``api.ingest`` against a shared
# ``note_key`` for both pre-compress fragments and the final session-end
# write. ``note_key`` upsert creates a new VERSION per write — only the
# latest is surfaced — so each flush silently overwrote the prior. The fix:
# first flush of the session creates the note; every subsequent flush goes
# through ``api.append_to_note`` with a stable, idempotent ``append_id``.
# These tests pin the new contract.


@pytest.fixture
def provider_with_append_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Like ``provider_with_stubbed_api`` but also stubs ``append_to_note``."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'test-vault')
    _write_test_config(tmp_path)

    fake_api = Mock()
    vault_uuid = uuid4()
    note_uuid = uuid4()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(return_value=vault_uuid)
    fake_api.get_session_briefing = AsyncMock(return_value='# Briefing')
    fake_api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(note_uuid)))
    fake_api.get_note = AsyncMock(return_value=SimpleNamespace(id=note_uuid))
    fake_api.head_note = AsyncMock(return_value=True)
    fake_api.append_to_note = AsyncMock(
        return_value=SimpleNamespace(
            status='success',
            note_id=note_uuid,
            append_id=uuid4(),
            content_hash='abc123',
            delta_bytes=10,
            new_unit_ids=[],
        )
    )
    fake_api.kv_put = AsyncMock()

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        provider = MemexMemoryProvider()
        provider.initialize('session-abc12345', hermes_home=str(tmp_path), platform='cli')
        yield provider, fake_api, vault_uuid
    provider.shutdown()


def _decode_ingest_body(api: Mock) -> str:
    import base64

    dto = api.ingest.call_args.args[0]
    return base64.b64decode(dto.content).decode('utf-8')


def _append_deltas(api: Mock) -> list[str]:
    """Read ``delta`` from each api.append_to_note call.

    ``append_to_note`` now takes unpacked kwargs (parity with MemexAPI), so
    the field lives on ``call.kwargs['delta']`` rather than on a Pydantic
    request object passed as ``call.args[0]``.
    """
    return [call.kwargs['delta'] for call in api.append_to_note.await_args_list]


def test_first_flush_creates_note_via_ingest(provider_with_append_api):
    """Single sync_turn → on_session_end ⇒ exactly one ingest, no appends."""
    provider, api, _ = provider_with_append_api
    provider.sync_turn('hi', 'hello')
    provider.sync_turn('ping', 'pong')
    provider.on_session_end([])
    _drain(provider)

    api.ingest.assert_awaited_once()
    api.append_to_note.assert_not_awaited()
    body = _decode_ingest_body(api)
    assert 'hi' in body and 'hello' in body
    assert 'ping' in body and 'pong' in body


def test_pre_compress_then_session_end_appends(provider_with_append_api):
    """Pre-compress writes the create; session-end appends the remainder."""
    provider, api, _ = provider_with_append_api
    provider.sync_turn('q1', 'a1')
    provider.sync_turn('q2', 'a2')

    provider.on_pre_compress([{'role': 'user', 'content': 'q1'}])
    _drain(provider)
    api.ingest.assert_awaited_once()
    api.append_to_note.assert_not_awaited()

    provider.sync_turn('q3', 'a3')
    provider.on_session_end([])
    _drain(provider)

    api.ingest.assert_awaited_once()  # still only one create
    assert api.append_to_note.await_count == 1
    kwargs = api.append_to_note.call_args.kwargs
    assert kwargs['note_key'] == provider._session_note_key
    assert kwargs['delta']  # non-empty
    assert 'q3' in kwargs['delta'] and 'a3' in kwargs['delta']


def test_pre_compress_does_not_clear_buffer(provider_with_append_api):
    """The buffer is retained verbatim; only the watermark advances."""
    provider, _api, _ = provider_with_append_api
    provider.sync_turn('q1', 'a1')
    provider.sync_turn('q2', 'a2')
    provider.sync_turn('q3', 'a3')
    assert len(provider._turn_buffer) == 3

    provider.on_pre_compress([{'role': 'user', 'content': 'q1'}])
    # Buffer length is preserved; watermark advanced past all three turns.
    assert len(provider._turn_buffer) == 3
    assert provider._flushed_index == 3


def test_two_pre_compresses_both_persist(provider_with_append_api):
    """Regression test for the original missing-chunks symptom.

    The reconstructed body across all writes (one ingest + N appends) must
    contain every turn verbatim, in order, with no overwrites.
    """
    provider, api, _ = provider_with_append_api

    # Compression 1 covers turns 1-2.
    provider.sync_turn('q1', 'a1')
    provider.sync_turn('q2', 'a2')
    provider.on_pre_compress([{'role': 'user', 'content': 'q1'}])

    # Compression 2 covers turns 3-4.
    provider.sync_turn('q3', 'a3')
    provider.sync_turn('q4', 'a4')
    provider.on_pre_compress([{'role': 'user', 'content': 'q3'}])

    # Final tail.
    provider.sync_turn('q5', 'a5')
    provider.on_session_end([])
    _drain(provider)

    assert api.ingest.await_count == 1
    assert api.append_to_note.await_count == 2

    body_parts = [_decode_ingest_body(api), *_append_deltas(api)]
    full_body = '\n\n'.join(body_parts)
    for token in ('q1', 'a1', 'q2', 'a2', 'q3', 'a3', 'q4', 'a4', 'q5', 'a5'):
        assert token in full_body, f'{token} missing from concatenated body'
    # Order preservation.
    assert full_body.index('q1') < full_body.index('q3') < full_body.index('q5')


def test_append_id_is_stable_for_retry(provider_with_append_api):
    """A failed append is retried with the SAME append_id on the next flush."""
    provider, api, _ = provider_with_append_api

    # First flush succeeds (create).
    provider.sync_turn('q1', 'a1')
    provider.on_pre_compress([{'role': 'user', 'content': 'q1'}])
    _drain(provider)
    api.ingest.assert_awaited_once()

    # Force the next append to fail.
    transient = RuntimeError('connection reset')
    api.append_to_note = AsyncMock(side_effect=transient)
    provider._api.append_to_note = api.append_to_note

    provider.sync_turn('q2', 'a2')
    provider.on_pre_compress([{'role': 'user', 'content': 'q2'}])
    _drain(provider)

    assert api.append_to_note.await_count == 1
    failed_append_id = api.append_to_note.call_args.kwargs['append_id']
    # Item stayed at the head of the queue.
    assert len(provider._pending) == 1
    assert provider._pending[0]['append_id'] == failed_append_id

    # Recover. The retry must use the SAME append_id (idempotent replay).
    api.append_to_note = AsyncMock(
        return_value=SimpleNamespace(
            status='replayed',
            note_id=uuid4(),
            append_id=failed_append_id,
            content_hash='x',
            delta_bytes=1,
            new_unit_ids=[],
        )
    )
    provider._api.append_to_note = api.append_to_note

    provider.sync_turn('q3', 'a3')
    provider.on_session_end([])
    _drain(provider)

    sent_append_ids = [c.kwargs['append_id'] for c in api.append_to_note.call_args_list]
    assert failed_append_id in sent_append_ids
    assert provider._pending == []


def test_session_end_with_empty_messages_falls_back_to_buffer(
    provider_with_append_api,
):
    """Hermes' contract permits ``messages=[]`` at session_end."""
    provider, api, _ = provider_with_append_api
    provider.sync_turn('only', 'turn')
    provider.on_session_end([])
    _drain(provider)
    api.ingest.assert_awaited_once()
    body = _decode_ingest_body(api)
    assert 'only' in body and 'turn' in body


def test_session_end_with_empty_buffer_falls_back_to_messages(
    provider_with_append_api,
):
    """Defense in depth: if sync_turn was never called but Hermes hands us
    a non-empty messages list at session_end, capture it as the create body."""
    provider, api, _ = provider_with_append_api
    provider.on_session_end(
        [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi back'},
        ]
    )
    _drain(provider)
    api.ingest.assert_awaited_once()
    body = _decode_ingest_body(api)
    assert 'hello' in body and 'hi back' in body


def test_session_end_with_empty_everything_is_noop(provider_with_append_api):
    """No turns synced, no messages — no write at all."""
    provider, api, _ = provider_with_append_api
    provider.on_session_end([])
    api.ingest.assert_not_awaited()
    api.append_to_note.assert_not_awaited()


def test_pre_compress_with_empty_buffer_is_noop(provider_with_append_api):
    """pre_compress trusts the local buffer; an empty buffer means nothing
    new to persist. Hermes' messages parameter is informational only here."""
    provider, api, _ = provider_with_append_api
    summary = provider.on_pre_compress([{'role': 'user', 'content': 'x'}])
    api.ingest.assert_not_awaited()
    api.append_to_note.assert_not_awaited()
    # Summary string is still returned for the compression prompt.
    assert provider._session_note_key in summary


def test_double_session_end_does_not_duplicate(provider_with_append_api):
    """Calling on_session_end twice in a row must not write twice.

    After the first session_end the buffer is cleared, so the second call
    finds nothing to flush and is a no-op.
    """
    provider, api, _ = provider_with_append_api
    provider.sync_turn('q', 'a')
    provider.on_session_end([])
    provider.on_session_end([])
    _drain(provider)
    api.ingest.assert_awaited_once()
    api.append_to_note.assert_not_awaited()


def test_create_failure_does_not_flip_initialized(provider_with_append_api):
    """If the first ingest raises, the note_key must stay OUT of
    _initialized_note_keys so the next flush retries the create — NOT skip
    ahead to append (which would fail because the note doesn't exist yet)."""
    provider, api, _ = provider_with_append_api
    api.ingest = AsyncMock(side_effect=RuntimeError('5xx'))
    provider._api.ingest = api.ingest

    provider.sync_turn('q', 'a')
    provider.on_session_end([])
    _drain(provider)

    assert provider._session_note_key not in provider._initialized_note_keys
    assert len(provider._pending) == 1
    assert provider._pending[0]['kind'] == 'create'

    # Recover. Next flush retries the create.
    api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(uuid4())))
    provider._api.ingest = api.ingest

    # Re-buffer some content to trigger a flush. Use shutdown's drain path.
    provider.shutdown()
    api.ingest.assert_awaited()
    assert provider._session_note_key in provider._initialized_note_keys


def test_pending_queue_capped_to_protect_memory(provider_with_append_api):
    """If Memex is unreachable for a long session, the queue must not grow
    without bound. Backpressure refuses NEW chunks at the cap so older
    content (more valuable for downstream reflection) is preserved."""
    provider, api, _ = provider_with_append_api
    api.ingest = AsyncMock(side_effect=RuntimeError('down'))
    api.append_to_note = AsyncMock(side_effect=RuntimeError('down'))
    provider._api.ingest = api.ingest
    provider._api.append_to_note = api.append_to_note

    from memex_hermes_plugin.memex.provider import _PENDING_MAX

    for i in range(_PENDING_MAX + 5):
        provider.sync_turn(f'q{i}', f'a{i}')
        provider.on_pre_compress([])
        _drain(provider)

    assert len(provider._pending) == _PENDING_MAX
    # The head must be the 'create' — appends would orphan onto a
    # non-existent note when Memex comes back.
    assert provider._pending[0]['kind'] == 'create'
    # Tail must be appends, in the order they were captured.
    assert all(p['kind'] == 'append' for p in provider._pending[1:])
    # First append is from turn 1; the last surviving append is from turn
    # (_PENDING_MAX - 1). Anything past that was refused at the boundary.
    assert 'q1' in provider._pending[1]['content']
    assert f'q{_PENDING_MAX - 1}' in provider._pending[-1]['content']
    # Verify the refused chunks were dropped, not silently inserted.
    for refused_idx in (_PENDING_MAX, _PENDING_MAX + 4):
        for entry in provider._pending:
            assert f'q{refused_idx}' not in entry.get('content', ''), (
                f'turn {refused_idx} should have been refused but is in queue'
            )


def test_recovery_after_outage_drains_queue(provider_with_append_api):
    """Memex returns after a transient outage; the queue drains in order."""
    provider, api, _ = provider_with_append_api

    api.ingest = AsyncMock(side_effect=RuntimeError('5xx'))
    api.append_to_note = AsyncMock(side_effect=RuntimeError('5xx'))
    provider._api.ingest = api.ingest
    provider._api.append_to_note = api.append_to_note

    # Build up backlog: 1 create + 3 appends, all failing.
    provider.sync_turn('q1', 'a1')
    provider.on_pre_compress([])
    provider.sync_turn('q2', 'a2')
    provider.on_pre_compress([])
    provider.sync_turn('q3', 'a3')
    provider.on_pre_compress([])
    provider.sync_turn('q4', 'a4')
    provider.on_pre_compress([])
    _drain(provider)

    assert len(provider._pending) == 4
    assert [p['kind'] for p in provider._pending] == ['create', 'append', 'append', 'append']

    # Recovery.
    api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(uuid4())))
    api.append_to_note = AsyncMock(
        return_value=SimpleNamespace(
            status='success',
            note_id=uuid4(),
            append_id=uuid4(),
            content_hash='x',
            delta_bytes=1,
            new_unit_ids=[],
        )
    )
    provider._api.ingest = api.ingest
    provider._api.append_to_note = api.append_to_note

    # Trigger drain via shutdown.
    provider.shutdown()

    api.ingest.assert_awaited_once()
    assert api.append_to_note.await_count == 3
    assert provider._pending == []


def test_non_transient_4xx_drops_failing_entry(provider_with_append_api):
    """A 409 / 422 / 400 / 404 cannot succeed on retry; drop the entry so
    the queue can keep draining. Otherwise one bad chunk poisons the rest.
    """
    import httpx

    provider, api, _ = provider_with_append_api

    # First flush succeeds (create).
    provider.sync_turn('q1', 'a1')
    provider.on_pre_compress([])
    _drain(provider)
    api.ingest.assert_awaited_once()

    # Next append: server returns 422 (delta validation failure).
    fake_response = httpx.Response(status_code=422, text='delta empty')
    fake_response._request = httpx.Request('POST', 'http://test/notes/append')
    bad_error = httpx.HTTPStatusError('422', request=fake_response._request, response=fake_response)
    api.append_to_note = AsyncMock(side_effect=bad_error)
    provider._api.append_to_note = api.append_to_note

    provider.sync_turn('q2', 'a2')
    provider.on_pre_compress([])
    _drain(provider)

    # Bad entry is dropped; queue is empty.
    assert provider._pending == []

    # Subsequent appends still work (queue is unblocked).
    api.append_to_note = AsyncMock(
        return_value=SimpleNamespace(
            status='success',
            note_id=uuid4(),
            append_id=uuid4(),
            content_hash='x',
            delta_bytes=1,
            new_unit_ids=[],
        )
    )
    provider._api.append_to_note = api.append_to_note
    provider.sync_turn('q3', 'a3')
    provider.on_pre_compress([])
    _drain(provider)
    api.append_to_note.assert_awaited_once()
    assert provider._pending == []


def test_transient_5xx_keeps_entry_for_retry(provider_with_append_api):
    """5xx and network errors are treated as transient; the entry stays
    queued for the next flush to retry with the same append_id.
    """
    import httpx

    provider, api, _ = provider_with_append_api

    provider.sync_turn('q1', 'a1')
    provider.on_pre_compress([])
    _drain(provider)

    fake_response = httpx.Response(status_code=503, text='busy')
    fake_response._request = httpx.Request('POST', 'http://test/notes/append')
    transient = httpx.HTTPStatusError('503', request=fake_response._request, response=fake_response)
    api.append_to_note = AsyncMock(side_effect=transient)
    provider._api.append_to_note = api.append_to_note

    provider.sync_turn('q2', 'a2')
    provider.on_pre_compress([])
    _drain(provider)

    # 503 is transient — entry stays at head.
    assert len(provider._pending) == 1
    assert provider._pending[0]['kind'] == 'append'


def test_vault_rebind_after_note_initialized_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Once we've created the session note in vault A, rebinding to vault B
    mid-session must NOT redirect subsequent appends — that would 404
    against vault B (note_key is vault-scoped) and silently split the
    transcript across two notes.
    """
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'vault-A')

    import json

    cfg_dir = tmp_path / 'memex'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'config.json').write_text(
        json.dumps(
            {
                'briefing_refresh_cadence': 1,
                'retain': {'min_capture_turns': 0, 'min_capture_chars': 0},
            }
        )
    )

    fake_api = Mock()
    vault_a = uuid4()
    vault_b = uuid4()
    note_uuid = uuid4()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(side_effect=[vault_a, vault_b])
    fake_api.get_session_briefing = AsyncMock(return_value='')
    fake_api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(note_uuid)))
    fake_api.get_note = AsyncMock(return_value=SimpleNamespace(id=note_uuid))
    fake_api.head_note = AsyncMock(return_value=True)

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        with patch(
            'memex_hermes_plugin.memex.provider.resolve_vault',
            side_effect=['vault-A', 'vault-B'],
        ):
            provider = MemexMemoryProvider()
            provider.initialize('s', hermes_home=str(tmp_path), platform='cli')
            try:
                # Land the create in vault A.
                provider.sync_turn('q', 'a')
                provider.on_session_end([])
                _drain(provider)
                assert provider._session_note_key in provider._initialized_note_keys
                assert provider._vault_id == vault_a

                # Trigger the rebind cadence; must be ignored since note is created.
                provider.on_turn_start(1, 'msg')
                assert provider._vault_id == vault_a
                assert provider._vault_name == 'vault-A'
            finally:
                provider.shutdown()


def test_wait_for_note_row_retries_on_transient_5xx(provider_with_append_api):
    """A 5xx during the post-create poll must NOT abort the wait — that
    would cause _drain_pending to leave the create queued and re-issue
    another ingest on the next flush, racing the original background job.
    Regression for round-2 FINDING-2.
    """
    import httpx

    provider, api, _ = provider_with_append_api

    fake_503 = httpx.Response(status_code=503, text='busy')
    fake_503._request = httpx.Request('HEAD', 'http://test/notes/x')
    transient = httpx.HTTPStatusError('503', request=fake_503._request, response=fake_503)

    # A.5: wait loop polls head_note now (cheap existence check).
    api.head_note = AsyncMock(side_effect=[transient, transient, True])
    provider._api.head_note = api.head_note

    assert provider._wait_for_note_row(provider._session_note_key, timeout=5.0) is True
    assert api.head_note.await_count == 3


def test_wait_for_note_row_bails_on_definitive_4xx(provider_with_append_api):
    """A non-404 4xx (e.g. 400/422) signals real misconfiguration — bail
    immediately so the caller can surface the failure rather than burning
    the full timeout polling a never-going-to-exist note.
    """
    import httpx

    provider, api, _ = provider_with_append_api

    fake_400 = httpx.Response(status_code=400, text='bad request')
    fake_400._request = httpx.Request('HEAD', 'http://test/notes/x')
    bad = httpx.HTTPStatusError('400', request=fake_400._request, response=fake_400)

    api.head_note = AsyncMock(side_effect=bad)
    provider._api.head_note = api.head_note

    assert provider._wait_for_note_row(provider._session_note_key, timeout=10.0) is False
    # Bailed early — only one call, not the full poll-loop count.
    assert api.head_note.await_count == 1


# ---- A.1: 409-on-create overlap path --------------------------------------


def test_create_409_waits_for_note_row_and_pops_on_success(provider_with_append_api):
    """A.1: 409 on create means an in-flight ingest of the same note_key is
    already running on the server (overlap detector). The note row will
    materialise once that job finishes — wait for it rather than dropping
    the head and orphaning every subsequent append.
    """
    import httpx

    provider, api, _ = provider_with_append_api

    # First ingest raises 409 + Location, second poll resolves.
    fake_409 = httpx.Response(
        status_code=409,
        text='overlap',
        headers={'Location': '/api/v1/ingestions/in-flight-id'},
    )
    fake_409._request = httpx.Request('POST', 'http://test/ingest')
    overlap = httpx.HTTPStatusError('409', request=fake_409._request, response=fake_409)
    api.ingest = AsyncMock(side_effect=overlap)
    provider._api.ingest = api.ingest
    # head_note (already AsyncMock returning True) resolves the wait.

    provider.sync_turn('q', 'a')
    # Defensive: confirm the flip happens via the 409→wait path, not from
    # leaked fixture state. Fixtures are function-scoped so this should be
    # impossible today, but a future refactor that shares state across
    # tests would silently turn this assertion into a no-op.
    assert provider._session_note_key not in provider._initialized_note_keys
    provider.on_pre_compress([])
    _drain(provider)

    # Head popped, note_key marked initialized, no orphaned create stays queued.
    assert provider._pending == []
    assert provider._session_note_key in provider._initialized_note_keys
    api.head_note.assert_awaited()


def test_create_409_keeps_head_queued_if_wait_times_out(provider_with_append_api):
    """A.1: if the in-flight ingest never materialises before the
    _wait_for_note_row deadline, leave the head in the queue. The next
    flush will retry — the alternative (dropping silently) is exactly the
    bug that caused the orphaned-session-note storm.
    """
    import httpx

    provider, api, _ = provider_with_append_api

    fake_409 = httpx.Response(
        status_code=409,
        text='overlap',
        headers={'Location': '/api/v1/ingestions/in-flight-id'},
    )
    fake_409._request = httpx.Request('POST', 'http://test/ingest')
    overlap = httpx.HTTPStatusError('409', request=fake_409._request, response=fake_409)
    api.ingest = AsyncMock(side_effect=overlap)
    provider._api.ingest = api.ingest

    # Force the wait to fail — make every head_note raise a non-transient 400
    # so the wait bails immediately rather than burning the default 120s.
    fake_400 = httpx.Response(status_code=400, text='nope')
    fake_400._request = httpx.Request('HEAD', 'http://test/notes/x')
    fail = httpx.HTTPStatusError('400', request=fake_400._request, response=fake_400)
    api.head_note = AsyncMock(side_effect=fail)
    provider._api.head_note = api.head_note

    provider.sync_turn('q', 'a')
    provider.on_pre_compress([])
    _drain(provider)

    # Head MUST stay; the note_key must NOT be marked initialized.
    assert len(provider._pending) == 1
    assert provider._pending[0]['kind'] == 'create'
    assert provider._session_note_key not in provider._initialized_note_keys


def test_append_409_still_drops(provider_with_append_api):
    """A.1's special-case is scoped to create. A 409 on append is still
    non-transient (an append should be idempotent on append_id; if the
    server rejects with 409 it won't accept a literal replay), so the
    drop-and-continue path is preserved.
    """
    import httpx

    provider, api, _ = provider_with_append_api

    # First flush succeeds (create).
    provider.sync_turn('q1', 'a1')
    provider.on_pre_compress([])
    _drain(provider)
    api.ingest.assert_awaited_once()

    # Next append: 409.
    fake_409 = httpx.Response(status_code=409, text='append conflict')
    fake_409._request = httpx.Request('POST', 'http://test/append')
    conflict = httpx.HTTPStatusError('409', request=fake_409._request, response=fake_409)
    api.append_to_note = AsyncMock(side_effect=conflict)
    provider._api.append_to_note = api.append_to_note

    provider.sync_turn('q2', 'a2')
    provider.on_pre_compress([])
    _drain(provider)

    # Append entry dropped (current contract — not the 409-create special case).
    assert provider._pending == []


# ---- A.2: exponential backoff in _wait_for_note_row -----------------------


def test_wait_for_note_row_uses_exponential_backoff(provider_with_append_api):
    """A.2: poll attempts space out per the backoff schedule (0.1, 0.2, 0.5,
    1.0, 2.0, 5.0) and then plateau at 10s. Previously a flat 0.1s sleep
    burned CPU + bounded the wait to 10s of attempts; new schedule keeps
    the early cycles snappy while bounding poll volume for multi-minute
    background extractions.
    """
    import httpx

    provider, api, _ = provider_with_append_api

    fake_503 = httpx.Response(status_code=503, text='busy')
    fake_503._request = httpx.Request('HEAD', 'http://test/notes/x')
    transient = httpx.HTTPStatusError('503', request=fake_503._request, response=fake_503)
    # Five transients then success — exercise the first five backoff steps.
    api.head_note = AsyncMock(
        side_effect=[transient, transient, transient, transient, transient, True]
    )
    provider._api.head_note = api.head_note

    sleeps: list[float] = []
    with patch(
        'memex_hermes_plugin.memex.provider.time.sleep',
        side_effect=lambda s: sleeps.append(s),
    ):
        assert provider._wait_for_note_row(provider._session_note_key, timeout=60.0) is True

    # Schedule: 0.1, 0.2, 0.5, 1.0, 2.0 — verify the prefix.
    assert sleeps[:5] == [0.1, 0.2, 0.5, 1.0, 2.0]


def test_backoff_delay_plateaus_after_schedule():
    """_backoff_delay returns the schedule values then plateaus."""
    from memex_hermes_plugin.memex.provider import (
        _WAIT_FOR_NOTE_ROW_BACKOFF,
        _WAIT_FOR_NOTE_ROW_BACKOFF_PLATEAU,
        _backoff_delay,
    )

    for i, expected in enumerate(_WAIT_FOR_NOTE_ROW_BACKOFF):
        assert _backoff_delay(i) == expected
    # Past the schedule end, return plateau.
    assert _backoff_delay(len(_WAIT_FOR_NOTE_ROW_BACKOFF)) == _WAIT_FOR_NOTE_ROW_BACKOFF_PLATEAU
    assert _backoff_delay(len(_WAIT_FOR_NOTE_ROW_BACKOFF) + 100) == (
        _WAIT_FOR_NOTE_ROW_BACKOFF_PLATEAU
    )


def test_wait_for_note_row_default_timeout_is_extended():
    """A.2: the default _wait_for_note_row deadline must comfortably exceed
    LLM-extraction p99 on the target hardware (Jetson Orin Nano, ~60s).
    """
    from memex_hermes_plugin.memex.provider import _WAIT_FOR_NOTE_ROW_DEFAULT_TIMEOUT

    assert _WAIT_FOR_NOTE_ROW_DEFAULT_TIMEOUT >= 60.0


# ---- A.6: CancelledError propagates ---------------------------------------


def test_drain_pending_propagates_cancelled_error(provider_with_append_api):
    """A.6: cooperative cancellation must not be swallowed.

    Gotcha: `run_sync` uses `asyncio.run_coroutine_threadsafe`, which
    translates `asyncio.CancelledError` raised inside the bridged
    coroutine into `concurrent.futures.CancelledError` on the calling
    thread (via `_chain_future` recognising the asyncio Task entered the
    cancelled state). `concurrent.futures.CancelledError` inherits from
    `Exception`, so the bare `except Exception` clause would swallow it
    without the explicit re-raise added by A.6.

    The public drain is now fire-and-forget (a daemon worker), so a
    cancellation raised inside it would surface on the worker thread, not
    the caller. We exercise the worker body — ``_drain_pending_sync`` — on
    this thread directly to assert the re-raise contract deterministically.
    """
    import asyncio
    import concurrent.futures

    provider, api, _ = provider_with_append_api
    api.ingest = AsyncMock(side_effect=asyncio.CancelledError())
    provider._api.ingest = api.ingest

    # Queue a create WITHOUT launching the async worker, so the drain runs
    # synchronously on this thread and the CancelledError is observable.
    with provider._state_lock:
        provider._pending.append(
            {
                'kind': 'create',
                'content': 'q\n\na',
                'title': 't',
                'note_key': provider._session_note_key,
                'vault_id': None,
            }
        )
    # Empirically run_sync surfaces concurrent.futures.CancelledError on the
    # caller thread (verified via a standalone repro: an asyncio Task whose
    # coroutine raises CancelledError enters the cancelled state, and
    # `_chain_future` translates that into concurrent.Future cancellation,
    # whose .result() raises concurrent.futures.CancelledError). The tuple
    # below admits the asyncio variant too — defensive against future
    # Python changes that unify the two classes, at zero cost.
    with pytest.raises((concurrent.futures.CancelledError, asyncio.CancelledError)):
        provider._drain_pending_sync()
    # Head still queued; future flush can retry once cancellation is handled.
    assert len(provider._pending) == 1
    assert provider._pending[0]['kind'] == 'create'

    # Let the fixture-teardown shutdown drain cleanly instead of re-raising
    # CancelledError on its background worker (which would surface as an
    # unhandled-thread-exception warning).
    provider._api.ingest = AsyncMock(
        return_value=SimpleNamespace(status='ok', note_id=str(uuid4()))
    )


def test_vault_rebind_with_pending_create_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A queued create with a snapshotted vault_id has effectively
    committed the session to that vault. If we let _vault_id drift to a
    new vault before the create lands, the create writes to vault A but
    every post-snapshot append targets vault B — note_key is vault-scoped,
    so vault-B appends 404, and the rebound transcript is silently lost.

    Regression for round-2 FINDING-1.
    """
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'vault-A')

    import json

    cfg_dir = tmp_path / 'memex'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'config.json').write_text(
        json.dumps(
            {
                'briefing_refresh_cadence': 1,
                'retain': {'min_capture_turns': 0, 'min_capture_chars': 0},
            }
        )
    )

    fake_api = Mock()
    vault_a = uuid4()
    vault_b = uuid4()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(side_effect=[vault_a, vault_b])
    fake_api.get_session_briefing = AsyncMock(return_value='')
    # Force the create to fail so it stays queued.
    fake_api.ingest = AsyncMock(side_effect=RuntimeError('down'))
    fake_api.get_note = AsyncMock(return_value=SimpleNamespace(id=uuid4()))

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        with patch(
            'memex_hermes_plugin.memex.provider.resolve_vault',
            side_effect=['vault-A', 'vault-B'],
        ):
            provider = MemexMemoryProvider()
            provider.initialize('s', hermes_home=str(tmp_path), platform='cli')
            try:
                # Queue a create against vault A (will fail).
                provider.sync_turn('q', 'a')
                provider.on_session_end([])
                _drain(provider)
                assert len(provider._pending) == 1
                assert provider._pending[0]['kind'] == 'create'
                assert provider._pending[0]['vault_id'] == str(vault_a)

                # Trigger rebind cadence; must be IGNORED because a create is queued.
                provider.on_turn_start(1, 'msg')
                assert provider._vault_id == vault_a
                assert provider._vault_name == 'vault-A'
                # And the queued create still targets vault A.
                assert provider._pending[0]['vault_id'] == str(vault_a)
            finally:
                provider.shutdown()


def test_vault_rebind_toctou_reverify_under_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If a create is enqueued WHILE _refresh_vault_binding's network
    calls are in flight, the second-pass check under the lock must reject
    the rebind. Otherwise the binding flips between the fast-path check
    and the mutation, splitting the transcript across vaults.

    Round-3 regression: simulate the race by patching resolve_vault to
    enqueue a create as a side effect.
    """
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'vault-A')

    import json

    cfg_dir = tmp_path / 'memex'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'config.json').write_text(
        json.dumps(
            {
                'briefing_refresh_cadence': 1,
                'retain': {'min_capture_turns': 0, 'min_capture_chars': 0},
            }
        )
    )

    fake_api = Mock()
    vault_a = uuid4()
    vault_b = uuid4()
    fake_api.kv_get = AsyncMock(return_value=None)
    fake_api.resolve_vault_identifier = AsyncMock(side_effect=[vault_a, vault_b])
    fake_api.get_session_briefing = AsyncMock(return_value='')
    fake_api.ingest = AsyncMock(side_effect=RuntimeError('down'))

    provider_holder: dict[str, Any] = {}

    def resolve_vault_side_effect(*args, **kwargs):
        # First call: returns vault-A (initial init).
        # Second call: simulate a concurrent flush enqueuing a create
        # WHILE we're mid-resolution.
        call_count = resolve_vault_mock.call_count
        if call_count == 2:
            p = provider_holder.get('p')
            if p is not None:
                p.sync_turn('q', 'a')
                p.on_session_end([])
                # A create is now queued (ingest fails, stays in queue).
        return ['vault-A', 'vault-B'][call_count - 1]

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        with patch(
            'memex_hermes_plugin.memex.provider.resolve_vault',
            side_effect=resolve_vault_side_effect,
        ) as resolve_vault_mock:
            provider = MemexMemoryProvider()
            provider.initialize('s', hermes_home=str(tmp_path), platform='cli')
            provider_holder['p'] = provider
            try:
                # No create queued yet; refresh_vault would normally rotate.
                # The side-effect injects a create during the network call.
                provider.on_turn_start(1, 'msg')
                _drain(provider)

                # The create should be queued (from the side-effect).
                assert any(p['kind'] == 'create' for p in provider._pending)
                # Vault binding must NOT have rotated to B — the
                # under-lock re-check rejected the rebind.
                assert provider._vault_id == vault_a
                assert provider._vault_name == 'vault-A'
            finally:
                provider.shutdown()


def test_shutdown_is_idempotent_under_concurrent_calls(provider_with_append_api):
    """Concurrent shutdown calls (Hermes' explicit shutdown + atexit
    fallback) must tear down exactly once. No double-close, no double-flush."""
    import threading

    provider, api, _ = provider_with_append_api
    provider.sync_turn('q', 'a')

    barrier = threading.Barrier(2)

    def call_shutdown():
        barrier.wait()
        provider.shutdown()

    t1 = threading.Thread(target=call_shutdown)
    t2 = threading.Thread(target=call_shutdown)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive() and not t2.is_alive()
    # ingest fired exactly once even under racing shutdowns.
    assert api.ingest.await_count == 1


def test_pending_survives_session_end_buffer_clear(provider_with_append_api):
    """on_session_end clears the buffer + watermark, but pending entries
    must survive — the queue is the durable client-side record once the
    chunk has been captured (not the buffer)."""
    provider, api, _ = provider_with_append_api

    # Make ingest fail so the entry stays queued.
    api.ingest = AsyncMock(side_effect=RuntimeError('down'))
    provider._api.ingest = api.ingest

    provider.sync_turn('q', 'a')
    provider.on_session_end([])
    _drain(provider)

    # Buffer cleared, but pending entry preserved.
    assert provider._turn_buffer == []
    assert provider._flushed_index == 0
    assert len(provider._pending) == 1
    assert provider._pending[0]['kind'] == 'create'
    assert 'q' in provider._pending[0]['content']


def test_vault_rebind_reresolves_on_cadence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Mid-session vault rebinds are honoured at the briefing-refresh cadence."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'vault-A')

    import json

    cfg_dir = tmp_path / 'memex'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'config.json').write_text(
        json.dumps(
            {
                'briefing_refresh_cadence': 2,
                'retain': {'min_capture_turns': 0, 'min_capture_chars': 0},
            }
        )
    )

    fake_api = Mock()
    vault_a = uuid4()
    vault_b = uuid4()
    fake_api.kv_get = AsyncMock(return_value=None)
    # First resolve returns vault-A, subsequent returns vault-B.
    fake_api.resolve_vault_identifier = AsyncMock(side_effect=[vault_a, vault_b])
    fake_api.get_session_briefing = AsyncMock(return_value='')
    fake_api.ingest = AsyncMock(return_value=SimpleNamespace(status='ok', note_id=str(uuid4())))

    with patch('memex_common.client.RemoteMemexAPI', return_value=fake_api):
        with patch(
            'memex_hermes_plugin.memex.provider.resolve_vault',
            side_effect=['vault-A', 'vault-B'],
        ):
            provider = MemexMemoryProvider()
            provider.initialize('s', hermes_home=str(tmp_path), platform='cli')
            try:
                assert provider._vault_name == 'vault-A'
                assert provider._vault_id == vault_a

                provider.on_turn_start(2, 'msg')
                # On a refresh-cadence boundary, the resolver re-runs and
                # vault binding follows the new value.
                assert provider._vault_name == 'vault-B'
                assert provider._vault_id == vault_b
            finally:
                provider.shutdown()


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
    # target='user' must land in the user: namespace (semantic intent
    # preserved so the mirror agrees with explicit memex_kv_put calls).
    assert kwargs['key'].startswith('user:hermes:')


def test_on_memory_write_remove_is_noop(provider_with_stubbed_api):
    provider, api, _ = provider_with_stubbed_api
    api.kv_put.reset_mock()
    provider.on_memory_write('remove', 'user', 'Prefers Rust')
    api.kv_put.assert_not_called()


def test_on_memory_write_memory_target_maps_to_app_namespace(provider_with_stubbed_api):
    """target='memory' is the agent's general scratchpad; it stays in app:hermes:memory:*."""
    provider, api, _ = provider_with_stubbed_api
    api.kv_put.reset_mock()
    provider.on_memory_write('add', 'memory', 'Build uses uv, not pip')
    api.kv_put.assert_awaited()
    kwargs = api.kv_put.call_args.kwargs
    assert kwargs['key'].startswith('app:hermes:memory:')


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

        retain: dict[str, Any] = {
            'min_capture_turns': 0,
            'min_capture_chars': 0,
        }
        if template is not None:
            retain['session_title_template'] = template
        cfg_dir = tmp_path / 'memex'
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / 'config.json').write_text(json.dumps({'retain': retain}))

        fake_api = Mock()
        note_uuid = uuid4()
        fake_api.kv_get = AsyncMock(return_value=None)
        fake_api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
        fake_api.get_session_briefing = AsyncMock(return_value='')
        fake_api.ingest = AsyncMock(
            return_value=SimpleNamespace(status='ok', note_id=str(note_uuid))
        )
        fake_api.get_note = AsyncMock(return_value=SimpleNamespace(id=note_uuid))
        fake_api.head_note = AsyncMock(return_value=True)
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
        _drain(p)
        try:
            api.ingest.assert_awaited()
            dto = api.ingest.call_args.args[0]
            # Title is no longer the hardcoded 'Hermes session'.
            assert dto.name != 'Hermes session'
            assert 'coder' in dto.name and 'cli' in dto.name
        finally:
            p.shutdown()

    def test_pre_compress_uses_session_title_no_fragment_marker(self, tmp_path, monkeypatch):
        """All writes to the session note share the same title.

        Regression for the original "missing chunks" bug: the prior
        implementation tagged pre-compress writes "(pre-compress fragment)"
        as a workaround for fragments overwriting each other. With ingest +
        idempotent appends, every write extends a single durable note, so
        the title stays consistent.
        """
        p, api = self._provider(tmp_path, monkeypatch)
        try:
            p.sync_turn('hi', 'hello')
            p.on_pre_compress([{'role': 'user', 'content': 'bye'}])
            _drain(p)
            api.ingest.assert_awaited()
            dto = api.ingest.call_args.args[0]
            assert 'fragment' not in dto.name
            assert 'coder' in dto.name and 'cli' in dto.name
        finally:
            p.shutdown()


# ---------------------------------------------------------------------------
# Async fire-and-forget drain + disk-spill durability
# ---------------------------------------------------------------------------


def _fake_ok_api():
    """A fake RemoteMemexAPI whose create succeeds and whose note row
    materialises only AFTER ingest (so the replay pre-create probe sees no
    row on first look, then the post-create wait sees one)."""
    api = Mock()
    note_uuid = uuid4()
    row = {'exists': False}
    api.kv_get = AsyncMock(return_value=None)
    api.resolve_vault_identifier = AsyncMock(return_value=uuid4())
    api.get_session_briefing = AsyncMock(return_value='')
    api.get_note = AsyncMock(return_value=SimpleNamespace(id=note_uuid))
    api.kv_put = AsyncMock()

    async def _ingest(dto, background=True):
        row['exists'] = True
        return SimpleNamespace(status='ok', note_id=str(note_uuid))

    api.ingest = AsyncMock(side_effect=_ingest)

    async def _head(note_id):
        return row['exists']

    api.head_note = AsyncMock(side_effect=_head)

    async def _append(**kwargs):
        return SimpleNamespace(
            status='success',
            note_id=note_uuid,
            append_id=uuid4(),
            content_hash='x',
            delta_bytes=1,
            new_unit_ids=[],
        )

    api.append_to_note = AsyncMock(side_effect=_append)
    return api


def _spill_file(tmp_path: Path) -> Path:
    from memex_hermes_plugin.memex.provider import _SPILL_FILE_NAME

    return tmp_path / 'memex' / _SPILL_FILE_NAME


def test_drain_launcher_is_non_blocking(provider_with_append_api):
    """_drain_pending spawns a daemon worker and returns; joining it drains."""
    import threading

    provider, api, _ = provider_with_append_api
    provider.sync_turn('q', 'a')
    provider.on_session_end([])
    worker = provider._bg_drain_thread
    assert isinstance(worker, threading.Thread)
    _drain(provider)
    api.ingest.assert_awaited_once()
    assert provider._pending == []


def test_spill_replay_lands_in_original_note_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression: a write spilled by a PREVIOUS session must replay under
    its OWN note_key, not the new session's (the cross-session contamination
    bug). Reproduced live before the fix: session A's transcript was written
    under session B's key."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    key_a = 'hermes:session:2020-01-01T00:00:00.000Z'
    spill = _spill_file(tmp_path)
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(
        json.dumps(
            [
                {
                    'kind': 'create',
                    'content': 'TRANSCRIPT-FROM-SESSION-A',
                    'title': 'Session A',
                    'note_key': key_a,
                    'vault_id': None,
                }
            ]
        )
    )

    api = _fake_ok_api()
    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        provider = MemexMemoryProvider()
        provider.initialize('session-B', hermes_home=str(tmp_path), platform='cli')
        try:
            assert provider._session_note_key != key_a
            _drain(provider)
            api.ingest.assert_awaited()
            dto = api.ingest.call_args.args[0]
            # The create landed under SESSION A's key, not session B's.
            assert dto.note_key == key_a
            import base64

            assert 'TRANSCRIPT-FROM-SESSION-A' in base64.b64decode(dto.content).decode()
        finally:
            provider.shutdown()
    # Spill file consumed on replay.
    assert not spill.exists()


def test_replayed_create_does_not_block_current_session_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Per-key gating: a replayed create for SESSION A must not make session
    B's first chunk enqueue as an append onto B's (not-yet-existent) note."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    key_a = 'hermes:session:2020-01-01T00:00:00.000Z'
    spill = _spill_file(tmp_path)
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(
        json.dumps(
            [{'kind': 'create', 'content': 'A', 'title': 'A', 'note_key': key_a, 'vault_id': None}]
        )
    )

    # Make ingest fail so the replayed A-create stays queued while we inspect
    # what session B enqueues.
    api = _fake_ok_api()
    api.ingest = AsyncMock(side_effect=RuntimeError('down'))
    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        provider = MemexMemoryProvider()
        provider.initialize('session-B', hermes_home=str(tmp_path), platform='cli')
        try:
            _drain(provider)  # A-create attempted, fails, stays queued.
            key_b = provider._session_note_key
            provider.sync_turn('q', 'a')
            provider.on_session_end([])
            _drain(provider)
            # Session B's first chunk is a CREATE for B, not an append.
            b_entries = [p for p in provider._pending if p['note_key'] == key_b]
            assert b_entries, 'session B enqueued nothing under its own key'
            assert b_entries[0]['kind'] == 'create'
        finally:
            provider.shutdown()


def test_undrained_writes_spill_to_disk_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If the server is unreachable, shutdown persists the pending queue to
    disk (with note_key) so it survives the restart."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    api = _fake_ok_api()
    api.ingest = AsyncMock(side_effect=RuntimeError('down'))
    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        provider = MemexMemoryProvider()
        provider.initialize('session-x', hermes_home=str(tmp_path), platform='cli')
        key = provider._session_note_key
        provider.sync_turn('q', 'a')
        provider.on_session_end([])
        _drain(provider)
        assert len(provider._pending) == 1
        provider.shutdown()

    spill = _spill_file(tmp_path)
    assert spill.exists()
    data = json.loads(spill.read_text())
    assert len(data) == 1
    assert data[0]['kind'] == 'create'
    assert data[0]['note_key'] == key


def test_clean_shutdown_leaves_no_spill_file(provider_with_append_api, tmp_path: Path):
    """On a healthy shutdown the worker drains everything, so no spill file
    is left behind."""
    provider, api, _ = provider_with_append_api
    provider.sync_turn('q', 'a')
    provider.on_session_end([])
    _drain(provider)
    provider.shutdown()
    assert not _spill_file(tmp_path).exists()


def test_shutdown_joins_worker_so_slow_write_drains_not_spills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A slow-but-successful ingest must be JOINED at shutdown (up to the
    bounded timeout) so it drains rather than getting spilled — proves the
    join-before-teardown ordering."""
    import asyncio

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    api = _fake_ok_api()
    row = {'exists': False}

    async def _slow_ingest(dto, background=True):
        await asyncio.sleep(0.2)
        row['exists'] = True
        return SimpleNamespace(status='ok', note_id=str(uuid4()))

    async def _head(note_id):
        return row['exists']

    api.ingest = AsyncMock(side_effect=_slow_ingest)
    api.head_note = AsyncMock(side_effect=_head)

    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        provider = MemexMemoryProvider()
        provider.initialize('session-x', hermes_home=str(tmp_path), platform='cli')
        provider.sync_turn('q', 'a')
        # Do NOT pre-drain; shutdown must join the in-flight worker itself.
        provider.shutdown()

    api.ingest.assert_awaited_once()
    assert not _spill_file(tmp_path).exists()


def test_replay_keeps_head_when_over_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An over-cap spill file must keep its HEAD (the leading create); the
    tail is dropped so append-before-create can't happen on replay."""
    from memex_hermes_plugin.memex.provider import _PENDING_MAX

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    key = 'hermes:session:2020-01-01T00:00:00.000Z'
    entries: list[dict[str, Any]] = [
        {'kind': 'create', 'content': 'HEAD', 'title': 't', 'note_key': key, 'vault_id': None}
    ]
    for i in range(_PENDING_MAX + 10):
        entries.append(
            {
                'kind': 'append',
                'content': f'a{i}',
                'append_id': str(uuid4()),
                'note_key': key,
                'vault_id': None,
            }
        )
    spill = _spill_file(tmp_path)
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(json.dumps(entries))

    # Fail all writes so the replayed queue stays intact for inspection.
    api = _fake_ok_api()
    api.ingest = AsyncMock(side_effect=RuntimeError('down'))
    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        provider = MemexMemoryProvider()
        provider.initialize('session-B', hermes_home=str(tmp_path), platform='cli')
        try:
            _drain(provider)
            assert len(provider._pending) == _PENDING_MAX
            assert provider._pending[0]['kind'] == 'create'
            assert provider._pending[0]['content'] == 'HEAD'
        finally:
            provider.shutdown()


def test_replayed_create_with_existing_row_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A replayed create whose row already exists server-side must NOT be
    re-ingested (which would mint a duplicate first-chunk-only version);
    it's marked initialized and popped, and its spilled appends still apply."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    key = 'hermes:session:2020-01-01T00:00:00.000Z'
    append_id = str(uuid4())
    spill = _spill_file(tmp_path)
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(
        json.dumps(
            [
                {
                    'kind': 'create',
                    'content': 'HEAD',
                    'title': 't',
                    'note_key': key,
                    'vault_id': None,
                },
                {
                    'kind': 'append',
                    'content': 'tail',
                    'append_id': append_id,
                    'note_key': key,
                    'vault_id': None,
                },
            ]
        )
    )

    api = _fake_ok_api()
    # Row already exists — the pre-create probe should short-circuit.
    api.head_note = AsyncMock(return_value=True)
    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        provider = MemexMemoryProvider()
        provider.initialize('session-B', hermes_home=str(tmp_path), platform='cli')
        try:
            _drain(provider)
            api.ingest.assert_not_awaited()  # no duplicate create
            api.append_to_note.assert_awaited_once()  # tail still applied
            assert api.append_to_note.call_args.kwargs['note_key'] == key
            assert provider._pending == []
        finally:
            provider.shutdown()


def test_corrupt_spill_file_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A garbage/partial spill file must not crash initialize."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    spill = _spill_file(tmp_path)
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text('{not valid json at all')

    api = _fake_ok_api()
    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        provider = MemexMemoryProvider()
        # Must not raise.
        provider.initialize('session-x', hermes_home=str(tmp_path), platform='cli')
        try:
            assert provider._pending == []
        finally:
            provider.shutdown()


def test_spill_roundtrips_append_uuid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A create + append queue must spill through _spill_to_disk (append_id
    UUID->str) and reload through _load_spill (str->UUID) so a multi-chunk
    session survives a restart and the reloaded append re-applies with the
    same idempotency id. This pins the most common real path (one create,
    N appends) — a regression there would silently lose whole sessions."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    # Session 1: server unreachable so both writes stay queued and spill.
    api1 = _fake_ok_api()
    api1.ingest = AsyncMock(side_effect=RuntimeError('down'))
    api1.append_to_note = AsyncMock(side_effect=RuntimeError('down'))
    with patch('memex_common.client.RemoteMemexAPI', return_value=api1):
        p1 = MemexMemoryProvider()
        p1.initialize('s1', hermes_home=str(tmp_path), platform='cli')
        key = p1._session_note_key
        p1.sync_turn('q1', 'a1')
        p1.on_pre_compress([])  # create
        _drain(p1)
        p1.sync_turn('q2', 'a2')
        p1.on_pre_compress([])  # append (create still pending → append)
        _drain(p1)
        assert [e['kind'] for e in p1._pending] == ['create', 'append']
        original_append_id = p1._pending[1]['append_id']
        assert isinstance(original_append_id, UUID)
        p1.shutdown()

    # The spill file went through _spill_to_disk: append_id is a str.
    spill = _spill_file(tmp_path)
    raw = json.loads(spill.read_text())
    assert [e['kind'] for e in raw] == ['create', 'append']
    assert raw[1]['append_id'] == str(original_append_id)

    # Session 2: server healthy → replay drains; append_id round-trips to UUID.
    api2 = _fake_ok_api()
    with patch('memex_common.client.RemoteMemexAPI', return_value=api2):
        p2 = MemexMemoryProvider()
        p2.initialize('s2', hermes_home=str(tmp_path), platform='cli')
        try:
            _drain(p2)
            assert api2.ingest.call_args.args[0].note_key == key
            api2.append_to_note.assert_awaited_once()
            akw = api2.append_to_note.call_args.kwargs
            assert akw['note_key'] == key
            assert akw['append_id'] == original_append_id
            assert p2._pending == []
        finally:
            p2.shutdown()
    assert not spill.exists()


def test_concurrent_enqueue_during_inflight_worker_drains_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Lost-wakeup handshake: a chunk enqueued WHILE the worker is mid-flight
    must be drained by that same worker (no strand), and no second worker is
    spawned. The create ingest is held until the second chunk is enqueued."""
    import asyncio
    import threading

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    api = _fake_ok_api()
    row = {'exists': False}
    gate = threading.Event()

    async def _ingest(dto, background=True):
        # Block the in-flight worker until the test enqueues the 2nd chunk.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, gate.wait, 2.0)
        row['exists'] = True
        return SimpleNamespace(status='ok', note_id=str(uuid4()))

    async def _head(note_id):
        return row['exists']

    api.ingest = AsyncMock(side_effect=_ingest)
    api.head_note = AsyncMock(side_effect=_head)

    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        p = MemexMemoryProvider()
        p.initialize('s', hermes_home=str(tmp_path), platform='cli')
        try:
            p.sync_turn('q1', 'a1')
            p.on_pre_compress([])  # create → worker starts, blocks in ingest
            worker = p._bg_drain_thread
            p.sync_turn('q2', 'a2')
            p.on_pre_compress([])  # append enqueued while worker is in-flight
            # Same worker must still be the one on record (no 2nd spawn).
            assert p._bg_drain_thread is worker
            gate.set()  # release the create ingest
            _drain(p)
            assert p._pending == []
            api.ingest.assert_awaited_once()
            api.append_to_note.assert_awaited_once()
        finally:
            gate.set()
            p.shutdown()


def test_drain_worker_swallows_cancellation_on_daemon_thread(provider_with_append_api):
    """The daemon-thread target must NOT let _PROPAGATE_EXCEPTIONS escape
    (which would crash the worker with an unhandled-thread-exception). The
    synchronous _drain_pending_sync still re-raises; the _drain_worker wrapper
    swallows it. Items stay queued for retry."""
    import asyncio

    provider, api, _ = provider_with_append_api
    api.ingest = AsyncMock(side_effect=asyncio.CancelledError())
    provider._api.ingest = api.ingest
    with provider._state_lock:
        provider._pending.append(
            {
                'kind': 'create',
                'content': 'q\n\na',
                'title': 't',
                'note_key': provider._session_note_key,
                'vault_id': None,
            }
        )
    # The thread target must return cleanly, not raise.
    provider._drain_worker()
    assert len(provider._pending) == 1
    # Let fixture teardown drain cleanly.
    provider._api.ingest = AsyncMock(
        return_value=SimpleNamespace(status='ok', note_id=str(uuid4()))
    )


def test_worker_start_failure_clears_marker_and_recovers(provider_with_append_api):
    """If threading.Thread.start() fails (resource exhaustion), the marker
    must be cleared so a later flush can spawn a fresh worker — otherwise the
    queue would grow without bound behind a dead marker."""
    import threading

    provider, api, _ = provider_with_append_api
    provider.sync_turn('q', 'a')

    with patch.object(
        threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")
    ):
        provider.on_session_end([])

    # Marker cleared; the chunk is still queued (never drained).
    assert provider._bg_drain_thread is None
    assert len(provider._pending) == 1

    # Recovery: a normal drain now spawns a worker and drains the backlog.
    provider._drain_pending()
    _drain(provider)
    api.ingest.assert_awaited_once()
    assert provider._pending == []


def test_spill_entry_with_non_str_vault_id_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A corrupt spill entry whose vault_id is not str|None must be dropped,
    not passed through to the server."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('MEMEX_SERVER_URL', 'http://test:8000')
    monkeypatch.setenv('MEMEX_VAULT', 'v')
    _write_test_config(tmp_path)

    spill = _spill_file(tmp_path)
    spill.parent.mkdir(parents=True, exist_ok=True)
    spill.write_text(
        json.dumps(
            [
                {
                    'kind': 'create',
                    'content': 'x',
                    'title': 't',
                    'note_key': 'k',
                    'vault_id': 123,  # not str|None
                }
            ]
        )
    )

    api = _fake_ok_api()
    with patch('memex_common.client.RemoteMemexAPI', return_value=api):
        provider = MemexMemoryProvider()
        provider.initialize('session-x', hermes_home=str(tmp_path), platform='cli')
        try:
            _drain(provider)
            assert provider._pending == []
            api.ingest.assert_not_awaited()
        finally:
            provider.shutdown()
