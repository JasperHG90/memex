"""Regression: ``list_notes`` ``--after`` filter must respect ``date_field``.

Reproduced production bug: a note ingested in March 2026 had a misextracted
``publish_date=2026-12-03``. ``memex note list --after 2026-04-23`` returned
that note because the server's ``COALESCE(publish_date, created_at)`` filter
saw the future publish_date and matched. Users (rightly) expected ``--after``
to filter on ingest time.

The fix introduced a ``date_field`` parameter on ``list_notes`` /
``get_recent_notes`` that selects which column to filter on. The CLI passes
``date_field='created_at'`` by default; HTTP/SDK callers default to
``'coalesce'`` for backward compatibility.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from memex_core.memory.sql_models import Note, Vault


@pytest.fixture
async def _seeded_notes(metastore):
    """Three notes with the temporal layout that triggered the bug:

    - ``ingested_long_ago``: created_at=Jan 1 2026, no publish_date.
    - ``ingested_today_future_publish``: created_at=Mar 28 2026 (recent),
      publish_date=Dec 3 2026 (in the future — the misextracted case).
    - ``ingested_today_no_publish``: created_at=Apr 23 2026 (today), no publish.
    """
    vault_id = uuid4()
    async with metastore.session() as session:
        session.add(Vault(id=vault_id, name='date-field-test'))
        await session.commit()

    ids = {
        'ingested_long_ago': uuid4(),
        'ingested_today_future_publish': uuid4(),
        'ingested_today_no_publish': uuid4(),
    }

    async with metastore.session() as session:
        session.add(
            Note(
                id=ids['ingested_long_ago'],
                content_hash='h1',
                vault_id=vault_id,
                original_text='old',
                doc_metadata={'name': 'Old note'},
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.add(
            Note(
                id=ids['ingested_today_future_publish'],
                content_hash='h2',
                vault_id=vault_id,
                original_text='tiinyai',
                doc_metadata={'name': 'TiinyAI-style note'},
                created_at=datetime(2026, 3, 28, tzinfo=timezone.utc),
                publish_date=datetime(2026, 12, 3, tzinfo=timezone.utc),
            )
        )
        session.add(
            Note(
                id=ids['ingested_today_no_publish'],
                content_hash='h3',
                vault_id=vault_id,
                original_text='today',
                doc_metadata={'name': 'Today note'},
                created_at=datetime(2026, 4, 23, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    return {'vault_id': vault_id, 'ids': ids}


@pytest.mark.asyncio
async def test_after_filter_default_is_coalesce_legacy(api, _seeded_notes):
    """Default ``date_field='coalesce'`` preserves the v0.1.14 server behaviour."""
    cutoff = datetime(2026, 4, 23, tzinfo=timezone.utc)
    notes = await api.list_notes(
        vault_ids=[_seeded_notes['vault_id']],
        after=cutoff,
    )
    ids = {n.id for n in notes}
    # Coalesce mode picks publish_date when set, so the future-publish note matches.
    assert _seeded_notes['ids']['ingested_today_future_publish'] in ids
    # The today note (no publish) matches via created_at fallback.
    assert _seeded_notes['ids']['ingested_today_no_publish'] in ids
    # The old note doesn't match either way.
    assert _seeded_notes['ids']['ingested_long_ago'] not in ids


@pytest.mark.asyncio
async def test_after_filter_created_at_excludes_misextracted_future_publish(api, _seeded_notes):
    """The bug fix: ``date_field='created_at'`` filters by ingest time only.

    The TiinyAI-style note (created_at in the past, publish_date in the
    future) MUST NOT appear in the results.
    """
    cutoff = datetime(2026, 4, 23, tzinfo=timezone.utc)
    notes = await api.list_notes(
        vault_ids=[_seeded_notes['vault_id']],
        after=cutoff,
        date_field='created_at',
    )
    ids = {n.id for n in notes}
    # Only the note actually created on/after the cutoff appears.
    assert ids == {_seeded_notes['ids']['ingested_today_no_publish']}


@pytest.mark.asyncio
async def test_after_filter_publish_date_excludes_notes_without_publish(api, _seeded_notes):
    """``date_field='publish_date'`` only matches notes with a publish_date set."""
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    notes = await api.list_notes(
        vault_ids=[_seeded_notes['vault_id']],
        after=cutoff,
        date_field='publish_date',
    )
    ids = {n.id for n in notes}
    # Only the note with publish_date matches; the others have NULL publish_date.
    assert ids == {_seeded_notes['ids']['ingested_today_future_publish']}


@pytest.mark.asyncio
async def test_before_filter_respects_date_field(api, _seeded_notes):
    cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    # Coalesce: the future-publish note is AFTER cutoff (Dec 3) → excluded.
    notes_coalesce = await api.list_notes(
        vault_ids=[_seeded_notes['vault_id']],
        before=cutoff,
    )
    coalesce_ids = {n.id for n in notes_coalesce}
    assert _seeded_notes['ids']['ingested_today_future_publish'] not in coalesce_ids

    # created_at: the future-publish note was ingested Mar 28 → BEFORE cutoff → included.
    notes_created = await api.list_notes(
        vault_ids=[_seeded_notes['vault_id']],
        before=cutoff,
        date_field='created_at',
    )
    created_ids = {n.id for n in notes_created}
    assert _seeded_notes['ids']['ingested_today_future_publish'] in created_ids


@pytest.mark.asyncio
async def test_invalid_date_field_raises_value_error(api, _seeded_notes):
    with pytest.raises(ValueError, match='Invalid date_field'):
        await api.list_notes(
            vault_ids=[_seeded_notes['vault_id']],
            after=datetime.now(timezone.utc) - timedelta(days=1),
            date_field='nope',
        )


@pytest.mark.asyncio
async def test_get_recent_notes_honours_date_field(api, _seeded_notes):
    """The ``recent`` CLI uses ``get_recent_notes`` — same fix needs to apply."""
    cutoff = datetime(2026, 4, 23, tzinfo=timezone.utc)
    recent = await api.get_recent_notes(
        vault_ids=[_seeded_notes['vault_id']],
        after=cutoff,
        date_field='created_at',
    )
    ids = {n.id for n in recent}
    assert ids == {_seeded_notes['ids']['ingested_today_no_publish']}
