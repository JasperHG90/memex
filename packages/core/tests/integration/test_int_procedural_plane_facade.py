"""Integration tests for the procedural-plane facade against real Postgres.

Covers the behaviours the unit tests can't — DB CHECK constraints, the
UNIQUE NULLS NOT DISTINCT identity-anchor index, the version row append
on update, the audit-event fire-and-forget, the search rank-RRF over
real tsvector + pgvector, and the pin-chain union at briefing time.

Each test seeds its own vault and runs against the testcontainer
Postgres that the ``metastore`` fixture exposes. The conftest's
``clean_tables`` autouse truncates between tests so suites are
order-independent.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from memex_common.procedural_schemas import (
    ProceduralEntryCreate,
    ProceduralPinCreate,
    ProceduralSearchRequest,
    ProceduralEntryUpdate,
)
from memex_core.services.procedural_repository import (
    ProceduralIdentityConflict,
    ProceduralRepository,
)
from memex_core.services.procedural_search_service import (
    ProceduralSearchService,
)

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_vault(session, name_prefix: str) -> uuid.UUID:
    """Mint a fresh content vault for the test (truncated by clean_tables
    between runs). The commit is required so a subsequent
    ``metastore.session()`` block — used by the repository / search
    service — can see the row."""
    vault_id = uuid.uuid4()
    await session.execute(
        text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
        {'id': str(vault_id), 'name': f'{name_prefix}_{vault_id.hex[:8]}'},
    )
    await session.commit()
    return vault_id


def _repo(metastore) -> ProceduralRepository:
    return ProceduralRepository(metastore=metastore)


def _search(metastore, embedding_model) -> ProceduralSearchService:
    repo = _repo(metastore)
    return ProceduralSearchService(
        metastore=metastore, repository=repo, embedding_model=embedding_model
    )


def _make_embedding_model() -> MagicMock:
    """A deterministic embedding model: all texts return the same
    384-dim unit-ish vector. Real search needs vector + tsvector to
    both contribute; identical vectors let the RRF rank be a function
    of the BM25 side alone, which is what these tests assert on."""
    model = MagicMock()

    def _encode(texts):
        import numpy as np

        return np.array([[0.1] * 384] * len(texts))

    model.encode.side_effect = _encode
    return model


def _entry_payload(
    *,
    vault_id: uuid.UUID,
    title: str,
    kind: str = 'procedure',
    scope: str = 'global',
    verb: str | None = 'deploy',
    context: str | None = 'staging',
    trigger: str | None = None,
    status: str = 'published',
    body: str = '',
) -> ProceduralEntryCreate:
    """Build a valid create payload for the procedural plane."""
    return ProceduralEntryCreate(
        vault_id=vault_id,
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        verb=verb,
        context=context,
        title=title,
        summary=f'Integration-seeded entry for {title!r}.',
        body=body,
        trigger=trigger,
        status=status,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Identity-anchor UNIQUE NULLS NOT DISTINCT — the load-bearing constraint
# that makes upsert_by_identity actually idempotent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_then_create_same_anchor_raises_identity_conflict(metastore):
    """Two procedures with the same (kind, scope, verb, context) anchor
    must not both exist — the second insert raises IdentityConflict.

    This pins the UNIQUE NULLS NOT DISTINCT index from migration 061:
    a regression that drops the index (or re-creates it with NULLS
    DISTINCT) would let duplicate anchors coexist, which would break
    upsert_by_identity and the briefing pin chain."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_idem')

    repo = _repo(metastore)
    await repo.create(_entry_payload(vault_id=vault_id, title=f'first-{uuid.uuid4()}'))

    with pytest.raises(ProceduralIdentityConflict):
        await repo.create(_entry_payload(vault_id=vault_id, title=f'second-{uuid.uuid4()}'))


@pytest.mark.asyncio
async def test_create_case_with_trigger_succeeds(metastore):
    """A case create with a non-null ``trigger`` succeeds. A previous
    migration added a CHECK ``(trigger IS NULL) = (trigger_embedding IS NULL)``
    on the assumption that the repository would set
    ``trigger_embedding`` on create — but the lazy-embedding design
    leaves it NULL. The CHECK fired on every case create, and the
    repository's blanket IntegrityError → IdentityConflict translation
    surfaced the constraint failure as a misleading 409. The CHECK
    was dropped (see migration 061 inline note); this test pins the
    happy path against a regression that re-introduces it.
    """
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_case_trigger')

    repo = _repo(metastore)
    entry = await repo.create(
        _entry_payload(
            vault_id=vault_id,
            kind='case',
            scope=f'case-{uuid.uuid4().hex[:8]}',
            verb=None,
            context=None,
            title='case-with-trigger',
            trigger='user reported slow API on 2026-05-01',
        )
    )
    assert entry.trigger == 'user reported slow API on 2026-05-01'
    # DTO does not surface embeddings — confirm the row round-tripped by
    # re-reading it via the repository. The lazy-embedding design leaves
    # trigger_embedding NULL on the row, and the dropped CHECK no longer
    # forces a paired-NULL invariant.
    fetched = await repo.get(entry.id, vault_id=vault_id)
    assert fetched is not None
    assert fetched.trigger == 'user reported slow API on 2026-05-01'


@pytest.mark.asyncio
async def test_upsert_on_existing_anchor_returns_merged_row(metastore):
    """A second upsert with the same anchor updates the existing row
    in place — the title/summary/body change but the id is stable.

    The id-stability property is what lets an agent re-write a
    procedure after learning something new, and the briefing pin chain
    (which references entries by id) keeps pointing at the same
    entry across rewrites.

    The version ledger is preserved across upsert: a re-write appends
    a new ``procedural_entry_versions`` row carrying the post-write
    body snapshot. The audit trail is reconstructable from
    ``procedural_entry_versions`` alone; a regression that dropped
    the version row would make upsert look invisible to incident
    response.
    """
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_upsert')

    repo = _repo(metastore)
    initial = await repo.upsert_by_identity(
        _entry_payload(vault_id=vault_id, title='upsert-initial', body='v1 body')
    )
    initial_id = initial.id
    initial_version_count = await _count_versions(metastore, initial_id)

    rewritten = await repo.upsert_by_identity(
        _entry_payload(vault_id=vault_id, title='upsert-rewritten', body='v2 body')
    )

    assert rewritten.id == initial_id, 'upsert must keep the same row id'
    assert rewritten.title == 'upsert-rewritten', 'upsert must update title'
    assert rewritten.body == 'v2 body', 'upsert must update body'
    post_upsert_version_count = await _count_versions(metastore, initial_id)
    # The upsert rewrite appends a new version row, mirroring
    # :meth:`update`. A regression that dropped the version row would
    # make the upsert look invisible to the version ledger.
    assert post_upsert_version_count == initial_version_count + 1


@pytest.mark.asyncio
async def test_upsert_preserves_deprecated_status(metastore):
    """Re-issuing an upsert on a deprecated entry does NOT undelete it.

    The route's contract is "deprecated stays deprecated" — a noisy
    caller that re-sends a stale ``status='draft'`` payload should not
    silently demote a superseded entry. A regression that overwrote
    the status unconditionally would let a buggy agent un-deprecate
    entries that the platform's deprecation path marked final.
    """
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_upsert_dep')

    repo = _repo(metastore)
    initial = await repo.upsert_by_identity(
        _entry_payload(vault_id=vault_id, title='upsert-dep-initial', body='v1 body')
    )
    deprecated = await repo.deprecate(initial.id, vault_id=vault_id)
    assert deprecated.status == 'deprecated'

    # Re-upsert with status='draft'. Status must NOT flip back.
    rewritten = await repo.upsert_by_identity(
        _entry_payload(
            vault_id=vault_id,
            title='upsert-dep-rewritten',
            body='v2 body',
            status='draft',
        )
    )
    assert rewritten.status == 'deprecated', (
        f'upsert must preserve deprecated status, got {rewritten.status!r}'
    )
    assert rewritten.title == 'upsert-dep-rewritten', 'other fields still update'


async def _count_versions(metastore, entry_id: uuid.UUID) -> int:
    async with metastore.session() as session:
        result = await session.execute(
            text('SELECT count(*) FROM procedural_entry_versions WHERE entry_id = :e'),
            {'e': str(entry_id)},
        )
    return int(result.scalar())


# ---------------------------------------------------------------------------
# Update — appends a version row, bumps updated_at, transitions
# published_at on first publish.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_appends_version_row_and_bumps_updated_at(metastore):
    """A successful update appends one row to ``procedural_entry_versions``
    and bumps ``updated_at`` on the entry. The version row carries
    the post-update body snapshot so the audit trail is reconstructable
    from the table alone."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_update')

    repo = _repo(metastore)
    entry = await repo.create(
        _entry_payload(vault_id=vault_id, title='update-target', body='original body')
    )
    assert entry.published_at is not None
    published_at_before = entry.published_at

    import asyncio

    # microsecond-resolution: updated_at must strictly increase
    await asyncio.sleep(0.01)

    updated = await repo.update(
        entry.id,
        ProceduralEntryUpdate(body='rewritten body', edit_reason='add detail'),
    )

    assert updated.body == 'rewritten body'
    assert updated.updated_at > entry.updated_at, 'updated_at must advance'
    # The published_at was already set on create; the update must
    # NOT clobber it (procedural contract: published_at is a one-way transition).
    assert updated.published_at == published_at_before

    version_count = await _count_versions(metastore, entry.id)
    assert version_count == 1, 'one update -> exactly one version row'


@pytest.mark.asyncio
async def test_update_without_fields_raises_value_error(metastore):
    """An all-empty update must fail loud, not silently no-op.

    A regression that dropped this guard would let a buggy caller
    'update' a row with no fields, leaving the operator convinced
    something had changed when in fact nothing had."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_update_empty')

    repo = _repo(metastore)
    entry = await repo.create(_entry_payload(vault_id=vault_id, title=f'noop-{uuid.uuid4()}'))
    with pytest.raises(ValueError, match='no fields set'):
        await repo.update(entry.id, ProceduralEntryUpdate())


# ---------------------------------------------------------------------------
# Deprecate — soft-deprecate with optional successor pointer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprecate_sets_status_and_superseded_by_pointer(metastore):
    """Deprecate transitions status→deprecated, optionally records the
    successor entry id, and bumps updated_at. The row stays in the
    table (soft deprecate) so the audit trail is intact."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_dep')

    repo = _repo(metastore)
    # Two distinct anchors: the deprecated one and its successor.
    # Sharing the (kind, scope, verb, context) would violate the
    # UNIQUE NULLS NOT DISTINCT identity anchor.
    old = await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title=f'old-{uuid.uuid4()}',
            verb='migrate',
            context='v1_to_v2',
        )
    )
    new = await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title=f'new-{uuid.uuid4()}',
            verb='migrate',
            context='v2_to_v3',
        )
    )

    deprecated = await repo.deprecate(old.id, superseded_by_id=new.id, vault_id=vault_id)

    assert deprecated.status == 'deprecated'
    assert deprecated.superseded_by_id == new.id
    assert deprecated.updated_at >= old.updated_at


# ---------------------------------------------------------------------------
# Search — the RRF fusion of BM25 + vector. The mock embedding
# returns identical vectors, so the ranking is driven by BM25.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_finds_published_procedure_via_bm25(metastore):
    """A published procedure with a distinctive title is findable via
    hybrid search; draft entries are hidden by the briefing-default
    status filter.

    A regression that flipped the default status to 'draft' (or
    dropped the BM25 index in migration 061) would make this test
    return zero hits."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_search')

    repo = _repo(metastore)
    # A unique token in the title lets BM25 find the row deterministically.
    token = f'uniqueproc{uuid.uuid4().hex[:8]}'
    await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title=f'procedure-{token}',
            verb='rollback',
            context=token,  # distinct context per test
            body=f'roll back the {token} migration safely',
        )
    )
    # A draft entry that must NOT show up in published-default search.
    # Distinct context (NOT the same as the published one) so the
    # UNIQUE identity anchor doesn't fire.
    await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title=f'procedure-{token}-draft',
            verb='rollback',
            context=f'{token}-draft',
            status='draft',
        )
    )

    svc = _search(metastore, _make_embedding_model())
    # Query with a token the body actually contains — the english
    # stemmer turns "rollback" / "roll" into different lexemes so a
    # query like "rollback" doesn't match a body containing "roll".
    response = await svc.search(ProceduralSearchRequest(query=f'migration {token}', limit=10))

    titles = [h.entry.title for h in response.hits]
    assert any(t == f'procedure-{token}' for t in titles), (
        f'expected to find the published procedure; got {titles!r}'
    )
    assert all('draft' not in t for t in titles), (
        f'draft entries leaked into published search: {titles!r}'
    )


@pytest.mark.asyncio
async def test_search_respects_kind_filter(metastore):
    """When ``kind='strategy'`` is requested, strategy hits are returned
    but procedures are not. A regression that dropped the kind
    filter would let procedures pollute the strategy briefing."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_kind')

    repo = _repo(metastore)
    await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title=f'proc-{uuid.uuid4().hex[:8]}',
            kind='procedure',
            verb='deploy',
            context='staging',
        )
    )
    strat_token = f'strat{uuid.uuid4().hex[:8]}'
    # The query and the title both contain "deploy" + token, so BM25
    # can match. The kind filter is the load-bearing assertion here;
    # we use "deploy" as the searchable term rather than the verb
    # field (which is not in the search_tsvector expression).
    await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title=f'deploy-{strat_token}',
            kind='strategy',
            verb='deploy',
            context='staging',
        )
    )

    svc = _search(metastore, _make_embedding_model())
    response = await svc.search(
        ProceduralSearchRequest(query=f'deploy {strat_token}', kind='strategy', limit=10)
    )

    assert response.hits, 'strategy filter returned no hits'
    assert all(h.entry.kind == 'strategy' for h in response.hits), (
        f'non-strategy entries leaked into strategy search: '
        f'{[h.entry.kind for h in response.hits]!r}'
    )


# ---------------------------------------------------------------------------
# Pin chain — briefing_cards unions pins across context keys, ordered
# by position ASC.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefing_cards_unions_pins_across_context_keys(metastore):
    """Pins across two context keys (e.g. 'global' + 'project:alpha')
    are unioned into a single ordered briefing, sorted by (context_key,
    position). This is the load-bearing read path the agent's
    briefing block consumes."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_pins')

    repo = _repo(metastore)
    e_global = await repo.create(
        _entry_payload(vault_id=vault_id, title='global-anchor', verb='anchor')
    )
    e_project = await repo.create(
        _entry_payload(vault_id=vault_id, title='project-anchor', scope='project:alpha')
    )

    await repo.add_pin(ProceduralPinCreate(context_key='global', entry_id=e_global.id, position=0))
    await repo.add_pin(
        ProceduralPinCreate(context_key='project:alpha', entry_id=e_project.id, position=0)
    )

    svc = _search(metastore, _make_embedding_model())
    cards = await svc.briefing_cards(context_keys=['global', 'project:alpha'], limit_per_context=5)

    assert len(cards.cards) == 2
    assert {c.context_key for c in cards.cards} == {'global', 'project:alpha'}
    assert {c.entry.id for c in cards.cards} == {e_global.id, e_project.id}
    # Position 0 for both — the briefing default is per-context
    # position, not absolute.
    assert all(c.pin_position == 0 for c in cards.cards)


# ---------------------------------------------------------------------------
# list_by_status — the enumeration surface search can't serve (drafts
# awaiting confirmation). A plain filtered SELECT, newest-first.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_by_status_filters_and_orders_newest_first(metastore):
    """``list_by_status`` enumerates by lifecycle state (the curation
    queue) where ``search`` short-circuits to empty without query text.

    Pins: status filter excludes other lifecycle states; kind/scope
    narrow further; ordering is created_at-descending so the freshest
    drafts surface first."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_list')

    repo = _repo(metastore)
    # Two draft procedures (distinct anchors), one published procedure,
    # one draft strategy — created in order so created_at is monotonic.
    d1 = await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title='draft-1',
            verb='deploy',
            context='staging',
            status='draft',
            trigger='when deploying to staging',
        )
    )
    d2 = await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title='draft-2',
            verb='rotate',
            context='creds',
            status='draft',
            trigger='when rotating credentials',
        )
    )
    pub = await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title='published-1',
            verb='audit',
            context='deps',
            status='published',
            trigger='when auditing dependencies',
        )
    )
    strat = await repo.create(
        _entry_payload(
            vault_id=vault_id,
            title='draft-strategy',
            kind='strategy',
            verb='release',
            context=None,
            status='draft',
            trigger='general release approach',
        )
    )

    # status='draft' → the three drafts, not the published entry.
    drafts = await repo.list_by_status(status='draft', vault_id=vault_id)
    assert {e.id for e in drafts} == {d1.id, d2.id, strat.id}
    assert pub.id not in {e.id for e in drafts}
    # Newest-first: created_at is non-increasing down the list.
    created = [e.created_at for e in drafts]
    assert created == sorted(created, reverse=True)

    # kind filter narrows to draft procedures only.
    draft_procs = await repo.list_by_status(status='draft', kind='procedure', vault_id=vault_id)
    assert {e.id for e in draft_procs} == {d1.id, d2.id}

    # status=None lists every lifecycle state.
    all_entries = await repo.list_by_status(vault_id=vault_id)
    assert {e.id for e in all_entries} == {d1.id, d2.id, pub.id, strat.id}

    # published filter isolates the one published entry.
    published = await repo.list_by_status(status='published', vault_id=vault_id)
    assert {e.id for e in published} == {pub.id}
