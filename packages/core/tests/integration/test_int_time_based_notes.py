"""Integration tests for time-based note listing (after/before filters)."""

import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import Note
from memex_core.server import app


def _make_note(
    *,
    title: str = 'Test',
    created_at: datetime | None = None,
    publish_date: datetime | None = None,
) -> Note:
    """Create a Note with a unique content_hash."""
    return Note(
        id=uuid4(),
        vault_id=GLOBAL_VAULT_ID,
        original_text=f'Content {uuid4()}',
        content_hash=str(uuid4()),
        title=title,
        created_at=created_at or datetime.now(timezone.utc),
        publish_date=publish_date,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_notes_after_date(api, metastore, init_global_vault):
    """Only notes on or after the given date are returned."""
    await api.initialize()

    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 6, 1, tzinfo=timezone.utc)

    async with metastore.session() as session:
        session.add(_make_note(title='old', created_at=old))
        session.add(_make_note(title='new', created_at=new))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 1
    assert results[0].title == 'new'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_notes_before_date(api, metastore, init_global_vault):
    """Only notes on or before the given date are returned."""
    await api.initialize()

    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 6, 1, tzinfo=timezone.utc)

    async with metastore.session() as session:
        session.add(_make_note(title='old', created_at=old))
        session.add(_make_note(title='new', created_at=new))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        before=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 1
    assert results[0].title == 'old'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_notes_date_range(api, metastore, init_global_vault):
    """Only notes within the after/before range are returned."""
    await api.initialize()

    dates = [
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        datetime(2025, 6, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    ]
    async with metastore.session() as session:
        for i, d in enumerate(dates):
            session.add(_make_note(title=f'note-{i}', created_at=d))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2025, 3, 1, tzinfo=timezone.utc),
        before=datetime(2025, 9, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 1
    assert results[0].title == 'note-1'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_notes_empty_range(api, metastore, init_global_vault):
    """When after > before, no notes match."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(_make_note(title='any', created_at=datetime(2025, 6, 1, tzinfo=timezone.utc)))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2026, 1, 1, tzinfo=timezone.utc),
        before=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coalesce_uses_publish_date(api, metastore, init_global_vault):
    """COALESCE prefers publish_date over created_at for filtering."""
    await api.initialize()

    async with metastore.session() as session:
        # created_at is 2026 but publish_date is 2024 — should NOT match after=2025
        session.add(
            _make_note(
                title='published-early',
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                publish_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coalesce_falls_back_to_created_at(api, metastore, init_global_vault):
    """When publish_date is None, COALESCE falls back to created_at."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(
            _make_note(
                title='no-publish-date',
                created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                publish_date=None,
            )
        )
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 1
    assert results[0].title == 'no-publish-date'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_recent_notes_with_date_filters(api, metastore, init_global_vault):
    """get_recent_notes respects after/before filters."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(_make_note(title='old', created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
        session.add(_make_note(title='new', created_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))
        await session.commit()

    results = await api.get_recent_notes(
        limit=10,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 1
    assert results[0].title == 'new'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timezone_aware_filtering(api, metastore, init_global_vault):
    """Timezone-aware datetimes are handled correctly."""
    await api.initialize()

    utc = timezone.utc
    est = timezone(timedelta(hours=-5))

    async with metastore.session() as session:
        # 2026-01-01 00:00 UTC
        session.add(
            _make_note(
                title='utc-note',
                created_at=datetime(2026, 1, 1, tzinfo=utc),
            )
        )
        await session.commit()

    # Query with EST — 2026-01-01 00:00 EST = 2026-01-01 05:00 UTC
    # The note (at midnight UTC) is *before* midnight EST, so it should be excluded
    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2026, 1, 1, tzinfo=est),
    )
    assert len(results) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_notes_date_rest_endpoint_invalid_date(api, metastore, init_global_vault):
    """REST endpoint returns 400 for invalid date strings."""
    await api.initialize()
    app.state.api = api

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/notes?after=not-a-date')
        assert response.status_code == 400
        assert 'after' in response.json()['detail'].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_notes_date_rest_endpoint_filters(api, metastore, init_global_vault):
    """REST endpoint correctly passes date filters to the API."""
    await api.initialize()
    app.state.api = api

    async with metastore.session() as session:
        session.add(_make_note(title='old', created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
        session.add(_make_note(title='new', created_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/notes?after=2026-01-01T00:00:00%2B00:00')
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 1
        assert data[0]['title'] == 'new'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_notes_date_rest_endpoint_invalid_before(api, metastore, init_global_vault):
    """REST endpoint returns 400 for invalid 'before' date string."""
    await api.initialize()
    app.state.api = api

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/notes?before=not-a-date')
        assert response.status_code == 400
        assert 'before' in response.json()['detail'].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_notes_date_rest_endpoint_both_invalid(api, metastore, init_global_vault):
    """REST endpoint returns 400 when 'after' is invalid (checked first)."""
    await api.initialize()
    app.state.api = api

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/notes?after=bad&before=also-bad')
        assert response.status_code == 400
        assert 'after' in response.json()['detail'].lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_notes_date_rest_endpoint_before_filter(api, metastore, init_global_vault):
    """REST endpoint filters notes using the 'before' parameter."""
    await api.initialize()
    app.state.api = api

    async with metastore.session() as session:
        session.add(_make_note(title='old', created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
        session.add(_make_note(title='new', created_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/notes?before=2025-06-01T00:00:00%2B00:00')
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 1
        assert data[0]['title'] == 'old'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_notes_date_rest_endpoint_sort_created_at(api, metastore, init_global_vault):
    """REST endpoint with sort=-created_at uses get_recent_notes with date filters."""
    await api.initialize()
    app.state.api = api

    async with metastore.session() as session:
        session.add(_make_note(title='old', created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
        session.add(_make_note(title='new', created_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        response = await ac.get('/api/v1/notes?sort=-created_at&after=2026-01-01T00:00:00%2B00:00')
        assert response.status_code == 200
        data = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert len(data) == 1
        assert data[0]['title'] == 'new'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_boundary_note_exactly_at_after(api, metastore, init_global_vault):
    """A note whose effective date equals 'after' is included (>= semantics)."""
    await api.initialize()

    boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async with metastore.session() as session:
        session.add(_make_note(title='boundary', created_at=boundary))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=boundary,
    )
    assert len(results) == 1
    assert results[0].title == 'boundary'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_boundary_note_exactly_at_before(api, metastore, init_global_vault):
    """A note whose effective date equals 'before' is included (<= semantics)."""
    await api.initialize()

    boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async with metastore.session() as session:
        session.add(_make_note(title='boundary', created_at=boundary))
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        before=boundary,
    )
    assert len(results) == 1
    assert results[0].title == 'boundary'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_database_date_filter(api, metastore, init_global_vault):
    """Date filtering on an empty database returns no results."""
    await api.initialize()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_notes_have_publish_date(api, metastore, init_global_vault):
    """When all notes have publish_date, COALESCE uses it for all filtering."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(
            _make_note(
                title='early',
                created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                publish_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.add(
            _make_note(
                title='late',
                created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                publish_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    # Filter by after=2025 — only 'late' (publish_date 2026-06) should match,
    # even though its created_at is 2024
    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 1
    assert results[0].title == 'late'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_notes_have_publish_date(api, metastore, init_global_vault):
    """When no notes have publish_date, COALESCE falls back to created_at for all."""
    await api.initialize()

    async with metastore.session() as session:
        session.add(
            _make_note(
                title='old',
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                publish_date=None,
            )
        )
        session.add(
            _make_note(
                title='new',
                created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                publish_date=None,
            )
        )
        await session.commit()

    results = await api.list_notes(
        limit=100,
        vault_ids=[GLOBAL_VAULT_ID],
        after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert len(results) == 1
    assert results[0].title == 'new'
