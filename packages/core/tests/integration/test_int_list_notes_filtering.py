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
    """status="archived" returns only archived notes."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(_make_note(title='active-note', status='active'))
        session.add(_make_note(title='archived-note', status='archived'))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        status='archived',
    )
    assert len(results) == 1
    assert results[0].title == 'archived-note'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_by_tags_and_status_combined(api, metastore, init_global_vault):
    """Combined tags + status filters both apply."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(_make_note(title='match', tags=['python'], status='active'))
        session.add(_make_note(title='wrong-status', tags=['python'], status='archived'))
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
