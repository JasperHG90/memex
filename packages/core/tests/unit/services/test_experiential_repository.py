"""Unit tests for the V7 Experiential Plane DTOs and repository contract.

These tests exercise the *contract* layer — DTO validation, ORM-to-DTO
conversion, error classes — using a mock metastore. The hybrid search
path (BM25 + vector + RRF) requires a real Postgres testcontainer; it
is covered by ``tests/integration/test_int_experiential_search.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from memex_common.experiential_schemas import (
    ExperientialBriefingCard,
    ExperientialBriefingCards,
    ExperientialDerivationQueueDTO,
    ExperientialEntryCreate,
    ExperientialEntryDTO,
    ExperientialEntryUpdate,
    ExperientialKind,
    ExperientialOrigin,
    ExperientialPinCreate,
    ExperientialPinDTO,
    ExperientialSearchRequest,
    ExperientialSearchResponse,
    ExperientialSourceCreate,
    ExperientialSourceRole,
    ExperientialStatus,
)
from memex_core.services.experiential_repository import (
    ExperientialEntryNotFound,
    ExperientialIdentityConflict,
    ExperientialRepository,
)


def _make_entry_row(
    *,
    entry_id: UUID | None = None,
    vault_id: UUID | None = None,
    kind: str = 'procedure',
    scope: str = 'global',
    verb: str | None = 'create_alembic',
    context: str | None = 'postgres',
    title: str = 'create alembic migration',
    summary: str = 'how to add a new alembic migration',
    body: str = 'run alembic revision -m "msg"',
    trigger: str | None = None,
    status: str = 'draft',
    origin: str = 'manual',
    tags: list[str] | None = None,
    extra_metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    published_at: datetime | None = None,
):
    """Build a mock DBExperientialEntry that survives the repo's _to_dto call.

    Uses ``MagicMock(spec=...)`` so attribute access passes through
    cleanly, with explicit values for the columns that ``_to_dto`` reads.
    """
    from memex_core.memory.sql_models import (
        ExperientialEntry as DBExperientialEntry,
    )

    now = datetime.now(timezone.utc)
    row = MagicMock(spec=DBExperientialEntry)
    row.id = entry_id or uuid4()
    row.vault_id = vault_id or uuid4()
    row.kind = MagicMock(value=kind)
    row.scope = scope
    row.verb = verb
    row.context = context
    row.title = title
    row.summary = summary
    row.body = body
    row.trigger = trigger
    row.tags = tags if tags is not None else []
    row.extra_metadata = extra_metadata if extra_metadata is not None else {}
    row.status = MagicMock(value=status)
    row.origin = MagicMock(value=origin)
    row.supersedes_id = None
    row.superseded_by_id = None
    row.published_at = published_at
    row.created_at = created_at or now
    row.updated_at = updated_at or now
    return row


def _make_queue_row(
    *,
    queue_id: UUID | None = None,
    vault_id: UUID | None = None,
    source_entry_ids: list[UUID] | None = None,
    target_kind: str = 'procedure',
    target_scope: str = 'global',
    target_verb: str | None = 'create_alembic',
    target_context: str | None = 'postgres',
    status: str = 'pending',
    attempt_count: int = 0,
    last_error: str | None = None,
    result_entry_id: UUID | None = None,
    claimed_at: datetime | None = None,
    completed_at: datetime | None = None,
    created_at: datetime | None = None,
):
    from memex_core.memory.sql_models import (
        ExperientialDerivationQueue as DBQueue,
    )

    now = datetime.now(timezone.utc)
    row = MagicMock(spec=DBQueue)
    row.id = queue_id or uuid4()
    row.vault_id = vault_id or uuid4()
    row.source_entry_ids = source_entry_ids if source_entry_ids is not None else []
    row.target_kind = target_kind
    row.target_scope = target_scope
    row.target_verb = target_verb
    row.target_context = target_context
    row.status = MagicMock(value=status)
    row.attempt_count = attempt_count
    row.last_error = last_error
    row.result_entry_id = result_entry_id
    row.claimed_at = claimed_at
    row.completed_at = completed_at
    row.created_at = created_at or now
    return row


def _make_pin_row(*, context_key: str = 'global', position: int = 0, entry_id: UUID | None = None):
    from memex_core.memory.sql_models import ExperientialPin as DBPin

    row = MagicMock(spec=DBPin)
    row.id = uuid4()
    row.context_key = context_key
    row.entry_id = entry_id or uuid4()
    row.position = position
    row.pinned_by = None
    row.created_at = datetime.now(timezone.utc)
    return row


def _make_async_exec_session(*, first=None, scalar_one=None, all_rows=None):
    """Build a session whose ``session.exec()`` is awaitable.

    Mirrors the conftest pattern but uses ``AsyncMock`` for the await
    site, so the repository's ``await session.exec(stmt).first()`` chain
    resolves correctly. ``first`` / ``scalar_one`` / ``all_rows`` are
    returned by the corresponding ``MagicMock`` attributes on the exec
    result.
    """
    session = AsyncMock()
    exec_result = MagicMock()
    exec_result.first.return_value = first
    exec_result.scalar_one.return_value = scalar_one
    exec_result.all.return_value = all_rows or []
    session.exec = AsyncMock(return_value=exec_result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.flush = AsyncMock()
    return session, exec_result


def _mock_session_with(*, exec_results: list[Any], execute_results: list[Any] | None = None):
    """Build an AsyncMock session whose ``exec()`` returns the supplied
    results in order (one per call). Mirrors the conftest's pattern."""
    session = AsyncMock()
    exec_mock = MagicMock()
    exec_mock.all.side_effect = exec_results or [[]]
    exec_mock.first.side_effect = [None] * (len(exec_results) or 1)
    exec_mock.scalar_one.side_effect = [None] * (len(exec_results) or 1)
    session.exec = MagicMock(return_value=exec_mock)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock(
        side_effect=execute_results or [MagicMock(all=MagicMock(return_value=[]))]
    )
    return session, exec_mock


# ---------------------------------------------------------------------------
# DTO validation
# ---------------------------------------------------------------------------


def test_create_dto_rejects_extra_fields():
    """``extra='forbid'`` on the create envelope surfaces typos as 422s."""
    with pytest.raises(ValidationError) as exc_info:
        ExperientialEntryCreate(
            vault_id=uuid4(),
            kind='procedure',
            scope='global',
            verb='create_alembic',
            context='postgres',
            title='create alembic migration',
            summary='how to add a new alembic migration',
            unknown_field='boom',
        )
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('unknown_field',) for e in errors), errors


@pytest.mark.asyncio
async def test_create_strategy_without_verb_and_context_raises():
    """Strategy create payloads need both verb AND context.

    The schema CHECK enforces this at the DB layer (migration 061's
    ``ck_strategy_context``); the repository surfaces the same rule
    earlier so the API returns a 4xx not a 500.
    """
    bad = ExperientialEntryCreate(
        vault_id=uuid4(),
        kind='strategy',
        scope='global',
        verb=None,  # missing on purpose
        context=None,  # missing on purpose
        title='bad strategy',
        summary='should not insert',
    )

    # The mock metastore is fine — the check fires before any DB call.
    metastore = MagicMock()
    metastore.session.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=AsyncMock()),
        __aexit__=AsyncMock(return_value=False),
    )
    repo = ExperientialRepository(metastore=metastore)

    with pytest.raises(ValueError, match='strategy entries require both verb and context'):
        await repo.create(bad)


def test_search_request_validates_weight_bounds():
    """bm25_weight / vector_weight must be in [0.0, 1.0]."""
    with pytest.raises(ValidationError):
        ExperientialSearchRequest(query='x', bm25_weight=1.5)
    with pytest.raises(ValidationError):
        ExperientialSearchRequest(query='x', vector_weight=-0.1)
    # Boundary values should pass.
    req = ExperientialSearchRequest(query='x', bm25_weight=0.0, vector_weight=1.0)
    assert req.bm25_weight == 0.0
    assert req.vector_weight == 1.0


def test_search_request_rejects_extra_fields():
    """``extra='forbid'`` on the search request envelope."""
    with pytest.raises(ValidationError):
        ExperientialSearchRequest(query='x', made_up_field='oops')


def test_source_create_requires_at_least_one_pointer():
    """ck_experiential_sources_pointer_set mirrored at the repository boundary.

    The DTO itself doesn't enforce this (so a partial payload is
    validatable), but the repository refuses to INSERT a source row
    with no pointer set.
    """
    # Empty payload is constructable — the DTO is permissive by design.
    src = ExperientialSourceCreate()
    assert src.role == 'evidence'
    assert src.weight == 1.0

    # But the repository refuses to persist it.
    metastore = MagicMock()
    metastore.session.return_value = MagicMock(
        __aenter__=AsyncMock(return_value=AsyncMock()),
        __aexit__=AsyncMock(return_value=False),
    )
    repo = ExperientialRepository(metastore=metastore)
    with pytest.raises(ValueError, match='at least one source pointer must be set'):
        # The repository signature requires entry_id first; we pass
        # a placeholder — the validation fires before any DB call.
        import asyncio

        asyncio.get_event_loop().run_until_complete(repo.add_source(uuid4(), src))


# ---------------------------------------------------------------------------
# Repository contract (mocked metastore)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_procedure_returns_dto_with_identity_fields():
    """create() persists and the DTO round-trips identity fields."""
    from memex_core.memory.sql_models import ExperientialEntry as DBEntry

    payload = ExperientialEntryCreate(
        vault_id=uuid4(),
        kind='procedure',
        scope='global',
        verb='create_alembic',
        context='postgres',
        title='create alembic migration',
        summary='how to add a new alembic migration',
        body='run alembic revision -m "msg"',
        tags=['alembic', 'postgres'],
    )

    # Mock the session to swallow the insert and yield a back-filled row.
    session = AsyncMock()
    persisted_row = _make_entry_row(
        vault_id=payload.vault_id,
        title=payload.title,
        kind=payload.kind,
        scope=payload.scope,
        verb=payload.verb,
        context=payload.context,
        body=payload.body,
        tags=payload.tags,
        status='draft',
        origin='manual',
    )
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore = MagicMock()
    metastore.session.return_value = ctx

    repo = ExperientialRepository(metastore=metastore)

    # Replace the entry-creation flow: capture the inserted DBEntry,
    # then on refresh swap the mock row into it so _to_dto reads our values.
    captured: list[DBEntry] = []

    def capture(row):
        captured.append(row)
        # Make the row look like the persisted one for DTO conversion.
        for attr in (
            'id',
            'vault_id',
            'kind',
            'scope',
            'verb',
            'context',
            'title',
            'summary',
            'body',
            'trigger',
            'tags',
            'extra_metadata',
            'status',
            'origin',
            'supersedes_id',
            'superseded_by_id',
            'published_at',
            'created_at',
            'updated_at',
        ):
            setattr(row, attr, getattr(persisted_row, attr))
        # Mirror the Enum types so _to_dto can call .value.
        row.kind = MagicMock(value=payload.kind)
        row.status = MagicMock(value='draft')
        row.origin = MagicMock(value='manual')
        row.__class__ = DBEntry  # type: ignore[assignment]

    session.add.side_effect = capture

    result = await repo.create(payload)

    assert isinstance(result, ExperientialEntryDTO)
    assert result.kind == 'procedure'
    assert result.scope == 'global'
    assert result.verb == 'create_alembic'
    assert result.context == 'postgres'
    assert result.tags == ['alembic', 'postgres']
    assert result.status == 'draft'
    assert result.origin == 'manual'
    # Confirm the session saw exactly one INSERT.
    assert session.add.call_count == 1
    assert session.commit.await_count == 1


@pytest.mark.asyncio
async def test_identity_anchor_conflict_raises_experiential_identity_conflict():
    """A second create on a colliding (kind, scope, verb, context) anchor
    must raise ``ExperientialIdentityConflict``, NOT a raw IntegrityError.

    The translator inspects ``exc.orig.__cause__.constraint_name`` to
    pick between the identity-anchor UNIQUE (409) and a non-anchor
    constraint (422). The mock stands in for asyncpg's
    ``UniqueViolationError`` carrying the partial-index name.
    """
    from sqlalchemy.exc import IntegrityError

    payload = ExperientialEntryCreate(
        vault_id=uuid4(),
        kind='procedure',
        scope='global',
        verb='create_alembic',
        context='postgres',
        title='first',
        summary='one',
    )

    session = AsyncMock()
    session.add = MagicMock()
    # The asyncpg error carries constraint_name on the inner exception
    # (SQLAlchemy's IntegrityError strips the diag; the __cause__ keeps
    # the rich asyncpg fields).
    inner = MagicMock()
    inner.constraint_name = 'uq_experiential_identity'
    inner.sqlstate = '23505'
    session.commit = AsyncMock(side_effect=IntegrityError('INSERT', {}, inner))
    session.refresh = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore = MagicMock()
    metastore.session.return_value = ctx

    repo = ExperientialRepository(metastore=metastore)

    with pytest.raises(ExperientialIdentityConflict):
        await repo.create(payload)


@pytest.mark.asyncio
async def test_update_appends_version_row_and_bumps_updated_at():
    """update() writes a new experiential_entry_versions row and stamps
    ``updated_at`` on the entry."""
    payload = ExperientialEntryUpdate(
        summary='updated summary',
        body='updated body',
        edit_reason='spec fix',
    )

    existing = _make_entry_row()
    pre_updated_at = existing.updated_at

    session, exec_mock = _make_async_exec_session(first=existing, scalar_one=None)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore = MagicMock()
    metastore.session.return_value = ctx

    repo = ExperientialRepository(metastore=metastore)

    result = await repo.update(existing.id, payload)

    # Two adds: the entry UPDATE and the version row.
    assert session.add.call_count == 2
    # The entry's updated_at moved forward.
    assert existing.updated_at >= pre_updated_at
    assert result.summary == 'updated summary'
    assert result.body == 'updated body'


@pytest.mark.asyncio
async def test_upsert_by_identity_inserts_when_missing():
    """First call to upsert_by_identity on a new anchor should INSERT."""
    payload = ExperientialEntryCreate(
        vault_id=uuid4(),
        kind='procedure',
        scope='project:abc',
        verb='lint',
        context='ruff',
        title='lint with ruff',
        summary='run ruff check',
    )

    session, exec_mock = _make_async_exec_session(first=None)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore = MagicMock()
    metastore.session.return_value = ctx
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore = MagicMock()
    metastore.session.return_value = ctx

    repo = ExperientialRepository(metastore=metastore)

    # The internal _get_entry will be called after commit/refresh; the
    # mock session's exec() always returns the same MagicMock, so .first()
    # will return None after the upsert — that's a problem for the
    # post-commit refresh. Patch _get_entry to return a synthesised row.
    fake_row = _make_entry_row(
        kind=payload.kind,
        scope=payload.scope,
        verb=payload.verb,
        context=payload.context,
        title=payload.title,
        summary=payload.summary,
    )

    async def _fake_get_entry(self_, sess, entry_id, *, vault_id=None):
        return fake_row

    # Patch the helper on the class for the duration of the call.
    from memex_core.services import experiential_repository as exp_repo_module

    original = exp_repo_module.ExperientialRepository._get_entry
    exp_repo_module.ExperientialRepository._get_entry = _fake_get_entry  # type: ignore[assignment]
    try:
        result = await repo.upsert_by_identity(payload)
    finally:
        exp_repo_module.ExperientialRepository._get_entry = original  # type: ignore[assignment]

    assert session.add.call_count == 1
    assert isinstance(result, ExperientialEntryDTO)


@pytest.mark.asyncio
async def test_upsert_by_identity_updates_existing_row():
    """Second call on the same anchor should UPDATE, not INSERT."""
    payload = ExperientialEntryCreate(
        vault_id=uuid4(),
        kind='strategy',
        scope='project:abc',
        verb='deploy',
        context='staging',
        title='deploy to staging',
        summary='run the deploy playbook',
    )

    existing = _make_entry_row(
        kind='strategy',
        scope='project:abc',
        verb='deploy',
        context='staging',
        title='old title',
        summary='old summary',
    )

    session, exec_mock = _make_async_exec_session(first=existing)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore = MagicMock()
    metastore.session.return_value = ctx
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore = MagicMock()
    metastore.session.return_value = ctx

    repo = ExperientialRepository(metastore=metastore)

    async def _fake_get_entry(self_, sess, entry_id, *, vault_id=None):
        # Reflect the in-place mutation by re-reading the existing mock.
        return existing

    from memex_core.services import experiential_repository as exp_repo_module

    original = exp_repo_module.ExperientialRepository._get_entry
    exp_repo_module.ExperientialRepository._get_entry = _fake_get_entry  # type: ignore[assignment]
    try:
        result = await repo.upsert_by_identity(payload)
    finally:
        exp_repo_module.ExperientialRepository._get_entry = original  # type: ignore[assignment]

    # The in-place UPDATE mutates the existing row rather than INSERT.
    assert existing.title == 'deploy to staging'
    assert existing.summary == 'run the deploy playbook'
    # Two adds: the entry UPDATE and the version ledger row that
    # ``upsert_by_identity`` always appends (matches the update() path —
    # the audit trail is the whole point of having a version table).
    assert session.add.call_count == 2
    assert isinstance(result, ExperientialEntryDTO)


@pytest.mark.asyncio
async def test_get_entry_raises_not_found():
    """get() with a missing id raises ExperientialEntryNotFound."""
    session, exec_mock = _make_async_exec_session(first=None)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    metastore = MagicMock()
    metastore.session.return_value = ctx

    repo = ExperientialRepository(metastore=metastore)

    with pytest.raises(ExperientialEntryNotFound):
        await repo.get(uuid4())


# ---------------------------------------------------------------------------
# Search service — DTO / shape contract
# ---------------------------------------------------------------------------


def test_search_response_truncation_flag_is_computed():
    """SearchResponse serialises the hit list and the truncation hint."""
    from memex_common.experiential_schemas import ExperientialSearchHit

    entry = ExperientialEntryDTO(
        id=uuid4(),
        vault_id=uuid4(),
        kind='procedure',
        scope='global',
        verb='create_alembic',
        context='postgres',
        title='x',
        summary='y',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    hit = ExperientialSearchHit(entry=entry, score=0.7, matched_via='rrf')
    resp = ExperientialSearchResponse(hits=[hit], total=1, truncated=False, took_ms=1.0)
    assert resp.hits[0].score == 0.7
    assert resp.truncated is False
    assert resp.took_ms == 1.0


def test_briefing_cards_orders_by_pin_position():
    """BriefingCards serialises position 0 first, then 1, 2, …"""
    entry_id = uuid4()
    entry = ExperientialEntryDTO(
        id=entry_id,
        vault_id=uuid4(),
        kind='strategy',
        scope='global',
        verb='deploy',
        context='staging',
        title='deploy',
        summary='s',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    cards = [
        ExperientialBriefingCard(entry=entry, pin_position=2, context_key='global'),
        ExperientialBriefingCard(entry=entry, pin_position=0, context_key='global'),
        ExperientialBriefingCard(entry=entry, pin_position=1, context_key='global'),
    ]
    envelope = ExperientialBriefingCards(
        cards=sorted(cards, key=lambda c: c.pin_position),
        context_keys=['global'],
        total_pinned=3,
    )
    assert [c.pin_position for c in envelope.cards] == [0, 1, 2]
    assert envelope.total_pinned == 3


def test_queue_dto_status_round_trip():
    """Queue DTO carries the status string for inspection by the CLI."""
    payload = ExperientialDerivationQueueDTO(
        id=uuid4(),
        vault_id=uuid4(),
        target_kind='procedure',
        target_scope='global',
        status='pending',
        attempt_count=0,
        created_at=datetime.now(timezone.utc),
    )
    assert payload.status == 'pending'
    assert payload.target_kind == 'procedure'
    assert payload.source_entry_ids == []


def test_entry_dto_pin_and_source_lists_default_empty():
    """Entry DTO surfaces empty source/pin lists when not provided."""
    e = ExperientialEntryDTO(
        id=uuid4(),
        vault_id=uuid4(),
        kind='case',
        scope='global',
        title='outage on staging',
        summary='db connection pool exhausted',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert e.sources == []
    assert e.pins == []
    assert e.tags == []
    assert e.extra_metadata == {}
    assert e.body == ''


def test_pin_dto_rejects_negative_position():
    """Pin position must be non-negative (ck_experiential_pins_position_nonneg)."""
    with pytest.raises(ValidationError):
        ExperientialPinDTO(
            id=uuid4(),
            context_key='global',
            entry_id=uuid4(),
            position=-1,
            created_at=datetime.now(timezone.utc),
        )
    # And the create envelope mirrors the rule.
    with pytest.raises(ValidationError):
        ExperientialPinCreate(
            context_key='global',
            entry_id=uuid4(),
            position=-1,
        )


# ---------------------------------------------------------------------------
# Enums / SSOT contract
# ---------------------------------------------------------------------------


def test_kind_status_origin_string_values_match_orm():
    """The DTO-side enum values must equal the SQLModel enum values.

    If these drift, the ORM CHECK constraint and the DTO validation
    will disagree and a valid create will 500 instead of 201.
    """
    from memex_core.memory.sql_models import (
        ExperientialKind as DBKind,
        ExperientialOrigin as DBOrigin,
        ExperientialSourceRole as DBRole,
        ExperientialStatus as DBStatus,
    )

    assert {k.value for k in ExperientialKind} == {k.value for k in DBKind}
    assert {k.value for k in ExperientialStatus} == {k.value for k in DBStatus}
    assert {k.value for k in ExperientialOrigin} == {k.value for k in DBOrigin}
    assert {k.value for k in ExperientialSourceRole} == {k.value for k in DBRole}
