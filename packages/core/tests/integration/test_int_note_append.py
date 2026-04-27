"""End-to-end integration tests for the atomic note-append endpoint (issue #56).

Mirrors the patterns from test_int_ingest_key.py: real Postgres via testcontainers,
api.memory.retain mocked through the existing patch_api_engines fixture, with
side_effect logic that mutates the Note row exactly the way real extraction
would (so the audit row, content_hash, and lineage are all verifiable).

These tests are the user-emphasized regression net for the feature.
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select

from memex_common.exceptions import (
    AppendIdConflictError,
    AppendLockTimeoutError,
    FeatureDisabledError,
    NoteNotAppendableError,
    NoteNotFoundError,
)
from memex_core.api import NoteInput
from memex_core.memory.sql_models import MemoryUnit, Note, NoteAppend, Vault
from memex_core.services.notes import derive_note_uuid_from_key
from memex_common.config import GLOBAL_VAULT_ID
from memex_common.types import FactTypes


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_retain_upsert(extra_unit_per_call: bool = True):
    """Build a retain mock that upserts the Note row and emits a unit per call.

    The mock simulates real extraction:
    - First call (existing note absent): inserts the Note with original_text.
    - Subsequent calls (note exists): updates original_text + content_hash to
      reflect the new combined body, and emits ONE new MemoryUnit row pointing
      at this note. That gives append tests something concrete to assert on.
    """

    async def _fake_retain(session, contents, note_id, **kwargs):  # type: ignore[no-untyped-def]
        content_item = contents[0]
        vault_id = content_item.vault_id
        note_uuid = UUID(note_id) if isinstance(note_id, str) else note_id
        new_hash = hashlib.md5(content_item.content.encode('utf-8')).hexdigest()

        await session.exec(
            pg_insert(Note)
            .values(
                id=note_uuid,
                title=content_item.payload.get('note_name'),
                description=content_item.payload.get('note_description', ''),
                content_hash=new_hash,
                vault_id=vault_id,
                original_text=content_item.content,
                publish_date=content_item.event_date,
            )
            .on_conflict_do_update(
                index_elements=['id'],
                set_={
                    'content_hash': new_hash,
                    'original_text': content_item.content,
                    'title': content_item.payload.get('note_name'),
                },
            )
        )

        unit_id: UUID | None = None
        if extra_unit_per_call:
            unit_id = uuid4()
            await session.exec(
                pg_insert(MemoryUnit).values(
                    id=unit_id,
                    note_id=note_uuid,
                    text=f'Extracted from {len(content_item.content)}-byte body',
                    fact_type=FactTypes.WORLD,
                    vault_id=vault_id,
                    embedding=[0.1] * 384,
                    event_date=content_item.event_date,
                )
            )
        return {
            'unit_ids': [str(unit_id)] if unit_id else [],
            'status': 'success',
            'touched_entities': set(),
        }

    return _fake_retain


async def _seed_parent_note(api, *, note_key: str, body: str, name: str = 'Session log') -> UUID:
    """Ingest a parent note via the real api.ingest path so all the subsequent
    test assertions go through the same plumbing as production."""
    api.memory.retain.side_effect = _make_retain_upsert()

    note = NoteInput(
        name=name,
        description='seed',
        content=body.encode('utf-8'),
        note_key=note_key,
    )
    result = await api.ingest(note)
    assert result['status'] == 'success'
    return UUID(str(result['note_id']))


# --------------------------------------------------------------------------- #
# Happy path — body grows, note_id stable                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_happy_path_in_place(api, metastore):
    """Append concatenates onto end of body; note_id is unchanged."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='session-1', body='# Day 1\n\nstart\n')

    result = await api.append_to_note(
        note_id=parent_id,
        delta='step 1: did the thing',
        append_id=uuid4(),
    )

    assert result['status'] == 'success'
    assert result['note_id'] == parent_id

    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc is not None
        assert doc.original_text.startswith('# Day 1\n\nstart\n')
        assert doc.original_text.endswith('step 1: did the thing')
        assert doc.content_hash == hashlib.md5(doc.original_text.encode()).hexdigest()


@pytest.mark.asyncio
async def test_append_audit_row_persisted(api, metastore):
    """Successful append leaves a NoteAppend audit row keyed on the append_id."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='session-2', body='hello')
    append_id = uuid4()

    await api.append_to_note(
        note_id=parent_id,
        delta='world',
        append_id=append_id,
        joiner='paragraph',
    )

    async with metastore.session() as session:
        row = (
            await session.exec(select(NoteAppend).where(col(NoteAppend.append_id) == append_id))
        ).first()
        assert row is not None
        assert row.note_id == parent_id
        assert row.delta_bytes == len(b'world')
        assert row.delta_sha256 == hashlib.sha256(b'world').hexdigest()
        assert row.joiner == 'paragraph'
        assert row.resulting_content_hash != ''
        assert len(row.new_unit_ids) == 1


# --------------------------------------------------------------------------- #
# Identifier resolution                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_by_note_key_round_trips(api, metastore):
    """create(note_key=X) then append(note_key=X, vault) finds the same row."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='kx-2026', body='body v1')

    expected_id = derive_note_uuid_from_key('kx-2026')
    assert parent_id == expected_id

    result = await api.append_to_note(
        note_key='kx-2026',
        vault_id=str(GLOBAL_VAULT_ID),
        delta='added line',
        append_id=uuid4(),
    )
    assert result['note_id'] == expected_id


@pytest.mark.asyncio
async def test_append_by_note_key_missing_vault_raises(api, metastore):
    """note_key without vault_id is rejected at the service layer."""
    from memex_common.exceptions import MemexError

    with pytest.raises(MemexError, match='vault_id is required'):
        await api.append_to_note(
            note_key='unrouted',
            vault_id=None,
            delta='x',
            append_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_append_with_both_identifiers_rejected(api, metastore):
    """Passing both note_id and note_key is rejected — service, schema, and CLI agree."""
    from memex_common.exceptions import MemexError

    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='primary', body='body')

    with pytest.raises(MemexError, match='Pass either note_id or note_key'):
        await api.append_to_note(
            note_id=parent_id,
            note_key='completely-different-key',
            vault_id=str(GLOBAL_VAULT_ID),
            delta='delta',
            append_id=uuid4(),
        )


# --------------------------------------------------------------------------- #
# Idempotent replay                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_idempotent_replay_returns_same_outcome(api, metastore):
    """Same append_id called twice → second call replays without re-mutating."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='replay-1', body='base')
    append_id = uuid4()

    first = await api.append_to_note(note_id=parent_id, delta='added', append_id=append_id)
    assert first['status'] == 'success'

    second = await api.append_to_note(note_id=parent_id, delta='added', append_id=append_id)
    assert second['status'] == 'replayed'
    assert second['note_id'] == parent_id
    assert second['append_id'] == append_id

    # Body grew exactly once.
    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc is not None
        assert doc.original_text.count('added') == 1
        rows = (
            await session.exec(select(NoteAppend).where(col(NoteAppend.note_id) == parent_id))
        ).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_append_replay_unicode_normalization_independent(api, metastore):
    """Same logical content sent first as NFD then as NFC must replay, not 409.

    The service hashes the NFC-normalised form for the conflict check, so a
    retry that an HTTP intermediary or alternate runtime has re-normalised
    must collide with the original on delta_sha256 and return ``replayed``.
    """
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='nfd-replay', body='base')
    append_id = uuid4()

    base = 'café'
    nfd_delta = unicodedata.normalize('NFD', base)
    nfc_delta = unicodedata.normalize('NFC', base)
    assert nfd_delta != nfc_delta
    assert len(nfd_delta) == 5 and len(nfc_delta) == 4

    first = await api.append_to_note(note_id=parent_id, delta=nfd_delta, append_id=append_id)
    assert first['status'] == 'success'

    second = await api.append_to_note(note_id=parent_id, delta=nfc_delta, append_id=append_id)
    assert second['status'] == 'replayed'
    assert second['note_id'] == parent_id
    assert second['append_id'] == append_id

    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc is not None
        assert doc.original_text.count(nfd_delta) == 1
        rows = (
            await session.exec(select(NoteAppend).where(col(NoteAppend.note_id) == parent_id))
        ).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_append_id_conflict_different_parent(api, metastore):
    """Same append_id with a different parent → AppendIdConflictError (409)."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_a = await _seed_parent_note(api, note_key='conflict-a', body='A')
    parent_b = await _seed_parent_note(api, note_key='conflict-b', body='B')
    append_id = uuid4()

    await api.append_to_note(note_id=parent_a, delta='x', append_id=append_id)
    with pytest.raises(AppendIdConflictError):
        await api.append_to_note(note_id=parent_b, delta='x', append_id=append_id)


@pytest.mark.asyncio
async def test_append_id_conflict_different_delta(api, metastore):
    """Same append_id, same parent, different delta → AppendIdConflictError."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='conflict-d', body='base')
    append_id = uuid4()

    await api.append_to_note(note_id=parent_id, delta='alpha', append_id=append_id)
    with pytest.raises(AppendIdConflictError):
        await api.append_to_note(note_id=parent_id, delta='beta', append_id=append_id)


@pytest.mark.asyncio
async def test_append_id_conflict_different_joiner(api, metastore):
    """Same append_id and delta but a different joiner → AppendIdConflictError.

    A retry that sneaks in a new joiner would silently produce a different body
    on the parent if we treated it as a replay; reject it as a caller bug.
    """
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='conflict-j', body='base')
    append_id = uuid4()

    await api.append_to_note(
        note_id=parent_id, delta='alpha', append_id=append_id, joiner='paragraph'
    )
    with pytest.raises(AppendIdConflictError):
        await api.append_to_note(
            note_id=parent_id, delta='alpha', append_id=append_id, joiner='newline'
        )


# --------------------------------------------------------------------------- #
# Concurrency                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concurrent_appends_serialise(api, metastore):
    """Five concurrent appends with distinct append_ids → all succeed sequentially.

    The advisory lock + SELECT FOR UPDATE force an order; the body grows by
    every delta and the audit table has 5 rows.
    """
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='concurrent-1', body='base')

    deltas = [f'd{i}' for i in range(5)]
    append_ids = [uuid4() for _ in range(5)]

    await asyncio.gather(
        *[
            api.append_to_note(note_id=parent_id, delta=d, append_id=aid)
            for d, aid in zip(deltas, append_ids)
        ]
    )

    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc is not None
        for d in deltas:
            assert d in doc.original_text
        rows = (
            await session.exec(select(NoteAppend).where(col(NoteAppend.note_id) == parent_id))
        ).all()
        assert len(rows) == 5


@pytest.mark.asyncio
async def test_concurrent_same_append_id_one_wins(api, metastore):
    """Five parallel posts with the SAME append_id: 1 success, 4 replays.

    This is the canonical idempotent retry scenario — the network glitch case.
    """
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='concurrent-same', body='base')
    append_id = uuid4()

    results: list[Any] = await asyncio.gather(
        *[
            api.append_to_note(note_id=parent_id, delta='only-once', append_id=append_id)
            for _ in range(5)
        ]
    )

    statuses = [r['status'] for r in results]
    assert len(statuses) == 5
    assert statuses.count('success') >= 1
    assert statuses.count('replayed') >= 1
    assert all(s in ('success', 'replayed') for s in statuses)

    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc is not None
        assert doc.original_text.count('only-once') == 1
        rows = (
            await session.exec(select(NoteAppend).where(col(NoteAppend.note_id) == parent_id))
        ).all()
        assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Lifecycle / status guards                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_to_archived_parent_raises(api, metastore):
    """Archived parents reject appends with NoteNotAppendableError."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='arch-1', body='b')

    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        doc.status = 'archived'
        session.add(doc)
        await session.commit()

    with pytest.raises(NoteNotAppendableError):
        await api.append_to_note(note_id=parent_id, delta='no', append_id=uuid4())


@pytest.mark.asyncio
async def test_append_to_superseded_parent_raises(api, metastore):
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='sup-1', body='b')

    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        doc.status = 'superseded'
        session.add(doc)
        await session.commit()

    with pytest.raises(NoteNotAppendableError):
        await api.append_to_note(note_id=parent_id, delta='no', append_id=uuid4())


@pytest.mark.asyncio
async def test_append_to_missing_parent_raises_not_found(api, metastore):
    fake_id = uuid4()
    with pytest.raises(NoteNotFoundError):
        await api.append_to_note(note_id=fake_id, delta='x', append_id=uuid4())


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_empty_delta_raises(api, metastore):
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='val-empty', body='b')
    with pytest.raises(ValueError, match='non-whitespace'):
        await api.append_to_note(note_id=parent_id, delta='   ', append_id=uuid4())


@pytest.mark.asyncio
async def test_append_delta_starting_with_frontmatter_raises(api, metastore):
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='val-fm', body='b')
    with pytest.raises(ValueError, match='frontmatter'):
        await api.append_to_note(
            note_id=parent_id,
            delta='---\nkey: val\n---\n',
            append_id=uuid4(),
        )


# --------------------------------------------------------------------------- #
# Joiners                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_joiner_paragraph(api, metastore):
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='joiner-p', body='line1')
    await api.append_to_note(
        note_id=parent_id,
        delta='line2',
        append_id=uuid4(),
        joiner='paragraph',
    )
    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc.original_text == 'line1\n\nline2'


@pytest.mark.asyncio
async def test_append_joiner_newline(api, metastore):
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='joiner-n', body='line1')
    await api.append_to_note(
        note_id=parent_id,
        delta='line2',
        append_id=uuid4(),
        joiner='newline',
    )
    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc.original_text == 'line1\nline2'


@pytest.mark.asyncio
async def test_append_joiner_none(api, metastore):
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='joiner-x', body='line1')
    await api.append_to_note(
        note_id=parent_id,
        delta='line2',
        append_id=uuid4(),
        joiner='none',
    )
    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc.original_text == 'line1line2'


@pytest.mark.asyncio
async def test_append_to_empty_parent_writes_only_delta(api, metastore):
    """Appending to a note with empty body lands the delta unchanged."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='empty-parent', body='')
    await api.append_to_note(
        note_id=parent_id,
        delta='first content',
        append_id=uuid4(),
    )
    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc.original_text == 'first content'


# --------------------------------------------------------------------------- #
# Kill switch / locking                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_kill_switch_returns_disabled_error(api, metastore):
    """server.append_enabled = False → FeatureDisabledError (maps to 503)."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='ks-1', body='b')

    api.config.server.append_enabled = False
    try:
        with pytest.raises(FeatureDisabledError):
            await api.append_to_note(note_id=parent_id, delta='x', append_id=uuid4())
    finally:
        api.config.server.append_enabled = True


@pytest.mark.asyncio
async def test_append_lock_acquire_timeout_raises(api, metastore, postgres_uri):
    """If another connection holds the parent's advisory lock, append times out."""
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='lock-1', body='b')

    api.config.server.append_lock_acquire_timeout_seconds = 0.5
    lock_key = parent_id.int & 0x7FFFFFFFFFFFFFFF

    # Open a separate raw asyncpg connection, hold the advisory lock, then fire
    # the append in parallel and expect AppendLockTimeoutError.
    from sqlalchemy.ext.asyncio import create_async_engine

    holder_engine = create_async_engine(postgres_uri, future=True)
    holder_started = asyncio.Event()
    holder_release = asyncio.Event()

    async def _holder() -> None:
        async with holder_engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': lock_key})
                holder_started.set()
                await holder_release.wait()
        await holder_engine.dispose()

    holder_task = asyncio.create_task(_holder())
    await holder_started.wait()
    try:
        with pytest.raises(AppendLockTimeoutError):
            await api.append_to_note(note_id=parent_id, delta='waited', append_id=uuid4())
    finally:
        holder_release.set()
        await holder_task
        api.config.server.append_lock_acquire_timeout_seconds = 30.0


# --------------------------------------------------------------------------- #
# Vault routing                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_vault_mismatch_raises_not_found(api, metastore):
    """note_key resolves but lives in a different vault → NoteNotFoundError."""
    api.memory.retain.side_effect = _make_retain_upsert()
    await _seed_parent_note(api, note_key='vault-mismatch', body='b')

    # Create a second vault and call with that vault_id
    async with metastore.session() as session:
        other = Vault(name='other-vault', description='for test')
        session.add(other)
        await session.commit()
        await session.refresh(other)
        other_id = other.id

    with pytest.raises(NoteNotFoundError):
        await api.append_to_note(
            note_key='vault-mismatch',
            vault_id=str(other_id),
            delta='x',
            append_id=uuid4(),
        )


# --------------------------------------------------------------------------- #
# user_notes carried but not re-injected                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_user_notes_lands_in_metadata(api, metastore):
    api.memory.retain.side_effect = _make_retain_upsert()
    parent_id = await _seed_parent_note(api, note_key='un-1', body='body')

    await api.append_to_note(
        note_id=parent_id,
        delta='step',
        append_id=uuid4(),
        user_notes='context: weekly retro',
    )

    async with metastore.session() as session:
        doc = await session.get(Note, parent_id)
        assert doc is not None
        assert (doc.doc_metadata or {}).get('user_notes') == 'context: weekly retro'
        # And it was NOT re-injected into the body.
        assert 'context: weekly retro' not in (doc.original_text or '')


# --------------------------------------------------------------------------- #
# Audit row rolled back on failure                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_audit_row_rolled_back_on_failure(api, metastore):
    """If memory.retain raises, the audit row must NOT persist (same txn)."""
    parent_id = await _seed_parent_note(api, note_key='rb-1', body='base')

    # Now make retain raise on the next call — simulating an extraction failure.
    api.memory.retain.side_effect = AsyncMock(side_effect=RuntimeError('boom'))
    append_id = uuid4()

    with pytest.raises(RuntimeError, match='boom'):
        await api.append_to_note(note_id=parent_id, delta='lost', append_id=append_id)

    async with metastore.session() as session:
        rows = (
            await session.exec(select(NoteAppend).where(col(NoteAppend.append_id) == append_id))
        ).all()
        assert rows == []  # rolled back atomically with the body change
        doc = await session.get(Note, parent_id)
        assert 'lost' not in (doc.original_text or '')
