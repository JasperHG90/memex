"""Integration tests for list_notes tag and status filtering (AC-010, AC-011)."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import Note


def _make_note(
    *,
    title: str = 'Test',
    tags: list[str] | None = None,
    status: str = 'active',
    archived_at: datetime | None = None,
) -> Note:
    """Create a Note with tags in doc_metadata and a given status."""
    return Note(
        id=uuid4(),
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Content {uuid4()}',
        content_hash=str(uuid4()),
        title=title,
        created_at=datetime.now(timezone.utc),
        doc_metadata={'tags': tags or []},
        status=status,
        archived_at=archived_at,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_tags_and_semantics(api, metastore, init_global_vault):
    """tags=["a","b"] returns only notes containing BOTH tags (AND semantics)."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(_make_note(title='both', tags=['a', 'b']))
        session.add(_make_note(title='only-a', tags=['a']))
        session.add(_make_note(title='b-and-c', tags=['b', 'c']))
        session.add(_make_note(title='no-tags', tags=[]))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        tags=['a', 'b'],
    )
    assert len(results) == 1
    assert results[0].title == 'both'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_single_tag(api, metastore, init_global_vault):
    """tags=["a"] returns all notes containing tag "a"."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(_make_note(title='has-a', tags=['a', 'x']))
        session.add(_make_note(title='no-a', tags=['b']))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        tags=['a'],
    )
    assert len(results) == 1
    assert results[0].title == 'has-a'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_status(api, metastore, init_global_vault):
    """``status='archived'`` returns notes with non-NULL ``archived_at``;
    ``status='active'`` excludes them. The archive intent is stored as
    ``status='active' AND archived_at IS NOT NULL`` rather than as a
    ``status`` enum value."""
    await api.initialize()
    now = datetime.now(timezone.utc)

    async with metastore.session() as session:
        session.add(_make_note(title='active-note', status='active'))
        session.add(_make_note(title='archived-note', status='active', archived_at=now))
        await session.commit()

    archived = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        status='archived',
    )
    assert len(archived) == 1
    assert archived[0].title == 'archived-note'

    active = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        status='active',
    )
    assert {n.title for n in active} == {'active-note'}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_tags_and_status_combined(api, metastore, init_global_vault):
    """Combined tags + status filters both apply (archived = archived_at IS NOT NULL)."""
    await api.initialize()
    now = datetime.now(timezone.utc)

    async with metastore.session() as session:
        session.add(_make_note(title='match', tags=['python'], status='active'))
        session.add(
            _make_note(title='wrong-status', tags=['python'], status='active', archived_at=now)
        )
        session.add(_make_note(title='wrong-tags', tags=['rust'], status='active'))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        tags=['python'],
        status='active',
    )
    assert len(results) == 1
    assert results[0].title == 'match'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slim_drops_per_note_summaries(api, metastore, init_global_vault):
    """V5: ``slim=True`` returns notes without per-block summaries; verbose
    surfaces them. Pinned via list_notes + get_recent_notes."""
    from memex_core.memory.sql_models import Chunk

    await api.initialize()

    note_ids = []
    async with metastore.session() as session:
        for i in range(3):
            note = _make_note(title=f'slim-{i}', tags=['s'])
            session.add(note)
            await session.flush()
            note_ids.append(note.id)
            for c in range(3):
                session.add(
                    Chunk(
                        note_id=note.id,
                        vault_id=GLOBAL_VAULT_ID,
                        chunk_index=c,
                        text=f'chunk text {c}',
                        content_hash=f'hash-{i}-{c}',
                        embedding=[0.1] * 384,
                        status='active',
                        summary={
                            'topic': f'topic-{i}-{c}',
                            'key_points': [
                                f'point-A-{i}-{c}',
                                f'point-B-{i}-{c}',
                            ],
                        },
                    )
                )
        await session.commit()

    verbose = await api.list_notes(limit=100, vault_ids=[GLOBAL_VAULT_ID], tags=['s'])
    slim = await api.list_notes(limit=100, vault_ids=[GLOBAL_VAULT_ID], tags=['s'], slim=True)

    assert len(verbose) == len(slim) == 3
    # Verbose carries 3 summaries per note (one per chunk).
    assert all(len(getattr(n, 'summaries', [])) == 3 for n in verbose)
    # Slim drops every summary.
    assert all(getattr(n, 'summaries', []) == [] for n in slim)

    recent_verbose = await api.get_recent_notes(limit=100, vault_ids=[GLOBAL_VAULT_ID])
    recent_slim = await api.get_recent_notes(limit=100, vault_ids=[GLOBAL_VAULT_ID], slim=True)
    assert {n.id for n in recent_verbose} >= set(note_ids)
    assert all(getattr(n, 'summaries', []) == [] for n in recent_slim if n.id in note_ids)
