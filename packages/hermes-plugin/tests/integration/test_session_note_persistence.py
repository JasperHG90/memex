"""Integration tests for the Hermes session-transcript persistence pipeline.

These tests run the real provider against a live Memex FastAPI server backed
by testcontainers Postgres. They pin the durability contract that the unit
tests cover with mocks: every turn synced into the buffer must end up in the
final note body verbatim, regardless of how many compression boundaries fire
in between.

Background: the prior implementation called ``api.ingest`` against a shared
``note_key`` for both pre-compress fragments and the final session-end write.
Each ``ingest`` created a new note version that REPLACED the prior content,
so multi-compression sessions ended up surfacing only the last fragment. The
unit tests assert the new ingest→append routing in isolation; these tests
prove it survives the full plugin <-> server <-> Postgres round-trip.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

pytestmark = pytest.mark.hermes_integration


# Background batch jobs persist the note asynchronously when ``ingest`` is
# called with ``background=True`` (the plugin's default for transcript
# writes). Polling avoids a sleep-based race.
_PERSIST_POLL_TIMEOUT = 15.0
_PERSIST_POLL_INTERVAL = 0.25


def _derive(note_key: str) -> UUID:
    from memex_core.services.notes import derive_note_uuid_from_key

    return derive_note_uuid_from_key(note_key)


async def _wait_for_note(live_api: Any, note_key: str) -> Any:
    """Poll until the note exists, then return its DTO."""
    note_id = _derive(note_key)
    deadline = asyncio.get_event_loop().time() + _PERSIST_POLL_TIMEOUT
    last_error: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            return await live_api.get_note(note_id)
        except Exception as e:
            last_error = e
            await asyncio.sleep(_PERSIST_POLL_INTERVAL)
    raise AssertionError(
        f'Note {note_id} (key={note_key}) did not appear within '
        f'{_PERSIST_POLL_TIMEOUT}s. Last error: {last_error!r}'
    )


async def _wait_until_body_contains(live_api: Any, note_key: str, *tokens: str) -> str:
    """Poll until the persisted body contains every token. Returns body."""
    note_id = _derive(note_key)
    deadline = asyncio.get_event_loop().time() + _PERSIST_POLL_TIMEOUT
    last_body = ''
    while asyncio.get_event_loop().time() < deadline:
        try:
            note = await live_api.get_note(note_id)
            body = note.original_text or ''
            if all(t in body for t in tokens):
                return body
            last_body = body
        except Exception:
            pass
        await asyncio.sleep(_PERSIST_POLL_INTERVAL)
    missing = [t for t in tokens if t not in last_body]
    raise AssertionError(
        f'Tokens {missing!r} not found in note body within '
        f'{_PERSIST_POLL_TIMEOUT}s. Last body length: {len(last_body)}'
    )


@pytest.mark.asyncio
async def test_full_lifecycle_persists_every_turn_verbatim(
    initialized_provider, live_api, live_vault: UUID
):
    """End-to-end: sync 5 turns, fire two pre-compresses, end the session.

    The persisted note body must be a verbatim superset of every synced turn.
    This is the regression test for the original "missing chunks" symptom.
    """
    p = initialized_provider

    p.sync_turn('user-q1', 'assistant-a1')
    p.sync_turn('user-q2', 'assistant-a2')
    p.on_pre_compress([{'role': 'user', 'content': 'user-q1'}])

    p.sync_turn('user-q3', 'assistant-a3')
    p.on_pre_compress([{'role': 'user', 'content': 'user-q3'}])

    p.sync_turn('user-q4', 'assistant-a4')
    p.sync_turn('user-q5', 'assistant-a5')
    p.on_session_end([])

    body = await _wait_until_body_contains(
        live_api,
        p._session_note_key,
        'user-q1',
        'assistant-a1',
        'user-q2',
        'assistant-a2',
        'user-q3',
        'assistant-a3',
        'user-q4',
        'assistant-a4',
        'user-q5',
        'assistant-a5',
    )
    # Order preservation across compressions.
    assert body.index('user-q1') < body.index('user-q3') < body.index('user-q5')

    note = await _wait_for_note(live_api, p._session_note_key)
    assert note.vault_id == live_vault


@pytest.mark.asyncio
async def test_session_end_only_path_creates_note(initialized_provider, live_api, live_vault: UUID):
    """Sessions without compression: single ingest, full body present."""
    p = initialized_provider

    p.sync_turn('hello', 'world')
    p.sync_turn('how', 'are you')
    p.on_session_end([])

    body = await _wait_until_body_contains(
        live_api, p._session_note_key, 'hello', 'world', 'how', 'are you'
    )
    assert body  # sanity
    note = await _wait_for_note(live_api, p._session_note_key)
    assert note.vault_id == live_vault


@pytest.mark.asyncio
async def test_append_id_replay_is_idempotent(initialized_provider, live_api, live_vault: UUID):
    """Replaying the same append_id must NOT double-write the body.

    Server-side idempotency is the safety net behind our retry list. Confirm
    it actually holds end-to-end.
    """
    from uuid import uuid4

    from memex_common.schemas import NoteAppendRequest

    p = initialized_provider
    p.sync_turn('first', 'turn')
    p.on_session_end([])
    # Wait for the create to land before issuing the manual appends.
    await _wait_for_note(live_api, p._session_note_key)

    append_id = uuid4()
    delta = '\n\nMANUAL_APPEND_MARKER\n'
    request = NoteAppendRequest(
        note_key=p._session_note_key,
        vault_id=str(live_vault),
        delta=delta,
        append_id=append_id,
        joiner='paragraph',
    )

    first = await live_api.append_to_note(request)
    second = await live_api.append_to_note(request)

    assert first.status == 'success'
    assert second.status == 'replayed'

    note = await _wait_for_note(live_api, p._session_note_key)
    body = note.original_text or ''
    assert body.count('MANUAL_APPEND_MARKER') == 1


@pytest.mark.asyncio
async def test_full_provider_lifecycle_end_to_end(initialized_provider, live_api, live_vault: UUID):
    """Drive every Hermes hook the plugin reacts to, in production order.

    Sequence: initialize (via fixture) → on_turn_start → sync_turn × N
    → on_pre_compress (twice) → on_session_end → shutdown.

    Asserts the persisted body is the verbatim ordered concatenation of
    every chunk that should have been flushed, with NO duplication and NO
    missing turns. This is the contract a session needs to honour for
    downstream retrieval / reflection to be trustworthy.
    """
    p = initialized_provider

    p.on_turn_start(turn_number=1, message='hi')
    p.sync_turn('what is the answer', '42')
    p.sync_turn('and the question', 'six times nine')
    p.on_turn_start(turn_number=2, message='compress now')

    p.on_pre_compress(
        [
            {'role': 'user', 'content': 'what is the answer'},
            {'role': 'assistant', 'content': '42'},
        ]
    )

    p.on_turn_start(turn_number=3, message='more')
    p.sync_turn('side question', 'tangent')
    p.on_pre_compress(
        [{'role': 'user', 'content': 'side question'}, {'role': 'assistant', 'content': 'tangent'}]
    )

    p.on_turn_start(turn_number=4, message='wrap up')
    p.sync_turn('final', 'goodbye')
    p.on_session_end([])

    body = await _wait_until_body_contains(
        live_api,
        p._session_note_key,
        'what is the answer',
        '42',
        'and the question',
        'six times nine',
        'side question',
        'tangent',
        'final',
        'goodbye',
    )
    # Exact-order check: the post-flush body must lay down chunks in the
    # order they were captured. Order matters for downstream LLM consumers.
    assert body.index('what is the answer') < body.index('side question') < body.index('final')
    # No duplication of any user-message phrase.
    for token in ('what is the answer', 'side question', 'final'):
        assert body.count(token) == 1, (
            f'token {token!r} appears {body.count(token)} times — expected 1'
        )
    # Note belongs to the right vault.
    note = await _wait_for_note(live_api, p._session_note_key)
    assert note.vault_id == live_vault

    # shutdown after session_end must be a no-op (buffer cleared).
    p.shutdown()


@pytest.mark.asyncio
async def test_pending_queue_drains_on_shutdown_after_recovery(
    loaded_provider, live_api, live_vault: UUID, hermes_home, vault_name: str, monkeypatch
):
    """If transient errors stack the pending queue, a clean shutdown drains it.

    We force initial attempts to fail by pointing the provider at a dead URL,
    populate the queue, then re-point at the live server and call shutdown.
    The body must contain every queued chunk.
    """
    monkeypatch.setenv('MEMEX_VAULT', vault_name)
    p = loaded_provider
    p.initialize(
        'integration-recovery-session',
        hermes_home=str(hermes_home),
        platform='cli',
        agent_identity='integration',
    )

    live_client = p._client
    live_api_real = p._api

    import httpx

    from memex_common.client import RemoteMemexAPI

    dead_client = httpx.AsyncClient(base_url='http://127.0.0.1:1/', timeout=0.5)
    p._client = dead_client
    p._api = RemoteMemexAPI(dead_client)

    p.sync_turn('q-a', 'r-a')
    p.on_pre_compress([])
    p.sync_turn('q-b', 'r-b')
    p.on_pre_compress([])
    p.sync_turn('q-c', 'r-c')
    p.on_pre_compress([])

    assert len(p._pending) == 3
    assert p._pending[0]['kind'] == 'create'

    await dead_client.aclose()
    p._client = live_client
    p._api = live_api_real

    # shutdown drains everything; live_api_real is restored so calls succeed.
    p.shutdown()

    body = await _wait_until_body_contains(
        live_api,
        p._session_note_key,
        'q-a',
        'r-a',
        'q-b',
        'r-b',
        'q-c',
        'r-c',
    )
    assert body
