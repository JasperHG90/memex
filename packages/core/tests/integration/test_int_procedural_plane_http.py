"""HTTP-layer integration tests for the procedural-plane routes.

The unit tests cover the route handlers in isolation (mocked
``MemexAPI.procedural``). This file drives the full FastAPI
stack against a real Postgres + pgvector, so it pins the wire
contract — the path an agent or HTTP client actually consumes.

Each test seeds a vault directly (raw SQL) so the test isn't
coupled to other MemexAPI flows, then drives the procedural
routes via ``ASGITransport`` and asserts the status codes and
response shapes. Auth is disabled at the dependency-overrides
layer so the tests focus on the load-bearing 4xx contracts:

* create → 201 / identity-anchor collision → 409
* upsert on existing anchor → 200 (idempotent rewrite)
* get_by_identity on unbound anchor → 200 with null
* briefing_cards returns the union of pinned entries across
  the requested context keys, ordered by position ascending
* deprecate on missing entry → 404 (not 500)
"""

from __future__ import annotations

import uuid
from typing import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_vault(session, name_prefix: str) -> uuid.UUID:
    vault_id = uuid.uuid4()
    await session.execute(
        text('INSERT INTO vaults (id, name) VALUES (:id, :name)'),
        {'id': str(vault_id), 'name': f'{name_prefix}_{vault_id.hex[:8]}'},
    )
    await session.commit()
    return vault_id


@pytest_asyncio.fixture
async def http_client(api) -> AsyncGenerator[httpx.AsyncClient, None]:
    """ASGI client wired to the test's own event loop (a sync TestClient
    would run requests on a second loop and trip the session-scoped
    async engine). Auth is disabled for the procedural-plane gate —
    authz is the F4 work, not the procedural contract under test here."""
    from memex_core.server import app
    from memex_core.server.auth import get_auth_context

    app.state.api = api
    app.dependency_overrides[get_auth_context] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        yield client
    if hasattr(app.state, 'api'):
        del app.state.api
    app.dependency_overrides.pop(get_auth_context, None)


def _payload(
    *,
    vault_id: uuid.UUID,
    title: str,
    kind: str = 'procedure',
    scope: str = 'global',
    verb: str | None = 'deploy',
    context: str | None = 'staging',
    trigger: str = 'when to exercise this integration-test entry',
    body: str = '',
) -> dict:
    """Build a valid create payload for the HTTP body."""
    return {
        'vault_id': str(vault_id),
        'kind': kind,
        'scope': scope,
        'verb': verb,
        'context': context,
        'title': title,
        'summary': f'Integration-test entry {title!r}.',
        'trigger': trigger,
        'body': body,
        'status': 'published',
    }


def _entry_dto(
    *,
    vault_id: uuid.UUID,
    title: str,
    kind: str = 'procedure',
    scope: str = 'global',
    verb: str | None = 'deploy',
    context: str | None = 'staging',
    trigger: str = 'when to exercise this integration-test entry',
    body: str = '',
):
    """Build an ``ProceduralEntryCreate`` DTO for direct repository seeding.

    The HTTP path goes through Pydantic's JSON coercion, so a plain dict
    is fine. The repository path does not — it requires a typed DTO
    instance. This helper is the typed variant of :func:`_payload`.
    """
    from memex_common.procedural_schemas import ProceduralEntryCreate

    return ProceduralEntryCreate(
        vault_id=vault_id,
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        verb=verb,
        context=context,
        title=title,
        summary=f'Integration-test entry {title!r}.',
        trigger=trigger,
        body=body,
        status='published',  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# create — 201 on success, 409 on identity-anchor collision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_procedural_returns_201_on_success(http_client, metastore):
    """A well-formed create returns 201 with the DTO body (the agent's
    audit log can read the id directly off the response)."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_http_create')

    resp = await http_client.post(
        '/api/v1/procedural', json=_payload(vault_id=vault_id, title='http-create-1')
    )
    assert resp.status_code == 200, resp.text  # FastAPI POST → 200 by default
    body = resp.json()
    assert body['kind'] == 'procedure'
    assert body['title'] == 'http-create-1'
    assert body['id'], 'create response must carry the new id'


@pytest.mark.asyncio
async def test_post_procedural_collision_returns_409(http_client, metastore):
    """A second create with the same (kind, scope, verb, context)
    anchor returns 409 (not 200, not 500) — the agent's write
    loop is supposed to switch to /upsert on a 409."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_http_409')

    first = await http_client.post(
        '/api/v1/procedural',
        json=_payload(
            vault_id=vault_id,
            title='http-409-first',
            verb='rotate',
            context='api_key',
        ),
    )
    assert first.status_code == 200, first.text

    second = await http_client.post(
        '/api/v1/procedural',
        json=_payload(
            vault_id=vault_id,
            title='http-409-second',
            verb='rotate',
            context='api_key',
        ),
    )
    assert second.status_code == 409, second.text
    # The first id is preserved — the agent can re-fetch the row.
    assert first.json()['id']


# ---------------------------------------------------------------------------
# upsert — idempotent rewrite on the identity anchor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_procedural_upsert_is_idempotent(http_client, metastore):
    """A second upsert with the same anchor returns 200 with the
    same id and the updated body. The agent's "I learned something
    new" loop re-writes in place without surfacing as an error."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_http_upsert')

    body = _payload(
        vault_id=vault_id,
        title='http-upsert-v1',
        verb='audit',
        context='quarterly',
        body='v1 body',
    )
    first = await http_client.post('/api/v1/procedural/upsert', json=body)
    assert first.status_code == 200, first.text
    first_id = first.json()['id']

    body['title'] = 'http-upsert-v2'
    body['body'] = 'v2 body'
    second = await http_client.post('/api/v1/procedural/upsert', json=body)
    assert second.status_code == 200, second.text
    assert second.json()['id'] == first_id, 'id must be stable across rewrites'
    assert second.json()['title'] == 'http-upsert-v2'
    assert second.json()['body'] == 'v2 body'


# ---------------------------------------------------------------------------
# get_by_identity — null on unbound (200, not 404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_identity_returns_null_on_unbound_anchor(http_client, metastore):
    """An unbound anchor returns 200 with a JSON null body — the
    "have we learned this?" probe must be cheap. A regression
    that flipped to 404 would break the agent's read-before-write
    decision tree."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_http_miss')

    resp = await http_client.get(
        '/api/v1/procedural/by-identity',
        params={
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'no_such_procedure',
            'context': 'no_such_context',
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() is None, f'unbound anchor must return null, got {resp.text!r}'


@pytest.mark.asyncio
async def test_get_by_identity_returns_entry_on_hit(http_client, metastore):
    """A bound anchor returns 200 with the DTO body — the read-before-
    write probe must surface a real hit, not silently None. A previous
    regression routed the lookup through ``api.procedural.search`` with
    no query text, which short-circuited to an empty response and broke
    the agent's idempotency check."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_http_hit')

    create = await http_client.post(
        '/api/v1/procedural',
        json=_payload(
            vault_id=vault_id,
            title='http-by-identity-hit',
            verb='by_identity_verb',
            context='by_identity_context',
        ),
    )
    assert create.status_code == 200, create.text
    created_id = create.json()['id']

    resp = await http_client.get(
        '/api/v1/procedural/by-identity',
        params={
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'by_identity_verb',
            'context': 'by_identity_context',
            'vault_id': str(vault_id),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body is not None, 'bound anchor must return the entry, not null'
    assert body['id'] == created_id
    assert body['title'] == 'http-by-identity-hit'
    assert body['verb'] == 'by_identity_verb'
    assert body['context'] == 'by_identity_context'


# ---------------------------------------------------------------------------
# briefing_cards — pin-chain union across context keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefing_cards_unions_pins_across_context_keys(http_client, metastore):
    """Two pins at two context keys come back as a single ordered
    briefing. The agent's briefing block reads ``cards`` in
    position order; a regression that returned per-context
    lists would break the flat-render assumption."""
    from memex_core.services.procedural_repository import ProceduralRepository
    from memex_common.procedural_schemas import ProceduralPinCreate

    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_http_briefing')

    repo = ProceduralRepository(metastore=metastore)
    e_global = await repo.create(
        _entry_dto(
            vault_id=vault_id,
            title='http-briefing-global',
            verb='anchor',
            context='global_anchor',
        )
    )
    e_project = await repo.create(
        _entry_dto(
            vault_id=vault_id,
            title='http-briefing-project',
            verb='anchor',
            context='project_anchor',
            scope='project:alpha',
        )
    )

    await repo.add_pin(ProceduralPinCreate(context_key='global', entry_id=e_global.id, position=0))
    await repo.add_pin(
        ProceduralPinCreate(context_key='project:alpha', entry_id=e_project.id, position=0)
    )

    resp = await http_client.post(
        '/api/v1/procedural/briefing-cards',
        json=['global', 'project:alpha'],
        params={'limit_per_context': 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {c['context_key'] for c in body['cards']} == {'global', 'project:alpha'}
    # The response carries str ids; the test seeds via the repository
    # (which returns UUID objects). Compare stringified UUIDs to avoid
    # an XYZ vs uuid.UUID mismatch on the assertion set.
    assert {c['entry']['id'] for c in body['cards']} == {str(e_global.id), str(e_project.id)}
    assert all(c['pin_position'] == 0 for c in body['cards'])


# ---------------------------------------------------------------------------
# deprecate — 404 on a missing id (not 500)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_deprecate_missing_entry_returns_404(http_client, metastore):
    """A deprecate on a non-existent id returns 404. A regression
    that raised ProceduralEntryNotFound (uncaught) would 500
    and look indistinguishable from a real DB error."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_http_dep_miss')

    missing_id = uuid.uuid4()
    resp = await http_client.post(
        f'/api/v1/procedural/{missing_id}/deprecate',
        params={'vault_id': str(vault_id)},
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# GET /procedural — list-by-status enumeration (the curation queue that
# /procedural/search can't serve without query text)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_procedural_lists_drafts_by_status(http_client, metastore):
    """``GET /procedural?status=draft`` enumerates the draft queue over
    the wire — the surface /curate consumes. Pins: the status filter
    excludes published entries; a bogus status is a 400, not a 500."""
    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'proc_http_list')

    draft = _payload(vault_id=vault_id, title='http-draft', verb='deploy', context='staging')
    draft['status'] = 'draft'
    published = _payload(vault_id=vault_id, title='http-pub', verb='rotate', context='creds')
    published['status'] = 'published'
    for body in (draft, published):
        resp = await http_client.post('/api/v1/procedural', json=body)
        assert resp.status_code == 200, resp.text

    listing = await http_client.get(
        '/api/v1/procedural', params={'status': 'draft', 'vault_id': str(vault_id)}
    )
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert [r['title'] for r in rows] == ['http-draft']
    assert all(r['status'] == 'draft' for r in rows)

    # A bogus lifecycle value is rejected by FastAPI validation (422),
    # not a handler 500.
    bad = await http_client.get('/api/v1/procedural', params={'status': 'bogus'})
    assert bad.status_code == 422, bad.text


# ---------------------------------------------------------------------------
# Multi-tenancy: a vault-restricted key cannot read/write across vaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_procedural_routes_enforce_vault_access(http_client, metastore):
    """A restricted key is fenced on the procedural plane: disallowed vault →
    403, omitted vault → 400 (would otherwise span all vaults), allowed vault
    → 200. Pins the B1 tenancy fix."""
    from memex_core.server import app
    from memex_core.server.auth import AuthContext, Permission, get_auth_context

    async with metastore.session() as session:
        allowed_vault = await _create_vault(session, 'proc_authz_ok')
        other_vault = await _create_vault(session, 'proc_authz_other')

    restricted = AuthContext(
        key_prefix='test',
        key_name='restricted',
        policy='reader',
        permissions=frozenset({Permission.READ, Permission.WRITE}),
        vault_ids=[str(allowed_vault)],
        read_vault_ids=None,
    )
    app.dependency_overrides[get_auth_context] = lambda: restricted
    try:
        # READ a disallowed vault → 403.
        r = await http_client.get('/api/v1/procedural', params={'vault_id': str(other_vault)})
        assert r.status_code == 403, r.text
        # READ with NO vault → 400 (restricted keys must name a vault).
        r = await http_client.get('/api/v1/procedural', params={'status': 'draft'})
        assert r.status_code == 400, r.text
        # READ the allowed vault → 200.
        r = await http_client.get('/api/v1/procedural', params={'vault_id': str(allowed_vault)})
        assert r.status_code == 200, r.text
        # WRITE into a disallowed vault → 403.
        r = await http_client.post(
            '/api/v1/procedural',
            json=_payload(vault_id=other_vault, title='authz-denied'),
        )
        assert r.status_code == 403, r.text
        # WRITE into the allowed vault → 200.
        r = await http_client.post(
            '/api/v1/procedural',
            json=_payload(vault_id=allowed_vault, title='authz-ok'),
        )
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides[get_auth_context] = lambda: None


@pytest.mark.asyncio
async def test_entry_pins_and_cases_enforce_vault_access(http_client, metastore, api):
    """The _authz_entry routes (versions/rollback/pin/unpin), the operator-only
    /procedural/pins, and /cases with a cross-vault case_of all fence a
    restricted key. Covers the gaps the _authz_vault test does not."""
    from memex_core.server import app
    from memex_core.server.auth import AuthContext, Permission, get_auth_context

    async with metastore.session() as session:
        allowed_vault = await _create_vault(session, 'authz2_ok')
        other_vault = await _create_vault(session, 'authz2_other')

    # Seed a procedure in the DISALLOWED vault via the repository (bypasses auth).
    other_entry = await api.procedural.create(
        _entry_dto(vault_id=other_vault, title='other-vault-entry', verb='rotate', context='creds')
    )

    restricted = AuthContext(
        key_prefix='test',
        key_name='restricted',
        policy='reader',
        permissions=frozenset({Permission.READ, Permission.WRITE}),
        vault_ids=[str(allowed_vault)],
        read_vault_ids=None,
    )
    app.dependency_overrides[get_auth_context] = lambda: restricted
    try:
        # entry-id route (_authz_entry) on an entry in a disallowed vault → 403.
        r = await http_client.get(f'/api/v1/procedural/{other_entry.id}/versions')
        assert r.status_code == 403, r.text
        # /procedural/pins is operator-only → 403 for any restricted key.
        r = await http_client.get('/api/v1/procedural/pins', params={'context_key': 'global'})
        assert r.status_code == 403, r.text
        # /cases with a case_of pointing at a disallowed vault's procedure → 403.
        r = await http_client.post(
            '/api/v1/cases',
            json={
                'title': 'cross-vault case',
                'trigger': 'tried to nudge another vault',
                'outcome': 'success',
                'scope': 'project:allowed',
                'scope_reasoning': 'Case submitted in the allowed vault context.',
                'case_of': str(other_entry.id),
            },
        )
        assert r.status_code == 403, r.text
    finally:
        app.dependency_overrides[get_auth_context] = lambda: None


# ---------------------------------------------------------------------------
# cases — full HTTP submit against real Postgres
#
# The integration `api` fixture mocks the extraction/memory ENGINES, but the
# IngestionService (which owns the content-hash idempotency gate) and the
# case service are real. Wiring `fake_retain_factory` makes the first ingest
# persist a real note with its content fingerprint, so the gate's skip path
# is exercised end-to-end; title/date LLM calls fail gracefully (no key
# needed), exactly as test_int_memex_api relies on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case_resubmit_is_content_idempotent(
    api, http_client, metastore, fake_retain_factory
):
    """Submitting byte-identical case content twice files the note once.

    The second POST returns 200 with assignment mode ``skipped`` and the SAME
    note id — NOT a 500 (the user-reported resubmit crash) and NOT a duplicate
    provenance edge. ``case_of`` takes the judge-free explicit path, so the
    flow is fully deterministic.
    """
    api.memory.retain.side_effect = fake_retain_factory

    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'case_idem')
    # Seed a published procedure to point case_of at (deterministic explicit
    # assignment — no LLM judge on submit).
    entry = await api.procedural.create(
        _entry_dto(vault_id=vault_id, title='case-idem-target', verb='deploy', context='prod')
    )

    body = {
        'title': 'Idempotent case submission',
        'trigger': 'resubmitted the exact same worked episode twice',
        'situation': 'verifying the content-addressed skip',
        'actions': ['ran the deploy', 'confirmed health checks'],
        'outcome': 'success',
        'lesson': 'identical content should file exactly once',
        'scope': 'project:alpha',
        'scope_reasoning': 'Idempotency test is scoped to project alpha.',
        'case_of': str(entry.id),
    }

    first = await http_client.post('/api/v1/cases', json=body)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body['assignment']['mode'] == 'explicit', first_body
    note_id = first_body['note_id']
    assert note_id

    # Byte-identical re-submit: content-addressed skip, 200 not 500, same id.
    second = await http_client.post('/api/v1/cases', json=body)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body['assignment']['mode'] == 'skipped', second_body
    assert second_body['note_id'] == note_id, 'skip must return the original note id'


@pytest.mark.asyncio
async def test_new_procedure_case_files_activation_proposal(
    api, http_client, metastore, fake_retain_factory
):
    """A case the judge rules a NEW procedure creates a DRAFT anchor AND files
    the governance lint item to activate it (draft → published via
    ``activate_procedural_entry``) at submission — so it is reviewable in the
    lint queue immediately, not only after the Phase-3 derivation worker. The
    judge is patched for determinism; ingest is real via ``fake_retain``.
    """
    from unittest.mock import AsyncMock, patch

    from memex_core.memory.procedural_assignment import AssignmentJudgment
    from memex_core.memory.sql_models import MaintenanceProposal, ProceduralEntry
    from memex_core.services.procedural_derivation_service import DISTILLATION_RULE_NAME

    api.memory.retain.side_effect = fake_retain_factory

    golden = AssignmentJudgment(
        decision='new_procedure',
        target_entry_id=None,
        proposed_verb='deploy',
        proposed_context='staging',
        separation='clean',
        runner_up=None,
        reasoning='no existing procedure matches this episode',
        proposed_scope='global',
        scope_separation='clean',
    )

    body = {
        'title': 'First time deploying to staging',
        'trigger': 'needed a brand-new staging deploy procedure',
        'outcome': 'success',
        'lesson': 'stage the config before flipping',
        'scope': 'global',
        'scope_reasoning': 'Staging deploy procedure should be global.',
    }

    with patch(
        'memex_core.services.case_service.judge_assignment',
        new=AsyncMock(return_value=golden),
    ):
        resp = await http_client.post('/api/v1/cases', json=body)

    assert resp.status_code == 200, resp.text
    rb = resp.json()
    assert rb['assignment']['mode'] == 'new_procedure_draft', rb
    entry_id = rb['assignment']['entry_id']
    finding_id = rb['assignment']['finding_id']
    assert entry_id, rb
    assert finding_id, 'activation proposal must be filed at submission'

    async with metastore.session() as session:
        # The anchor is a draft (invisible to search/briefing until activated).
        entry = await session.get(ProceduralEntry, uuid.UUID(entry_id))
        assert entry is not None and entry.status == 'draft', entry
        # A pending governance proposal targets the draft for activation.
        proposal = await session.get(MaintenanceProposal, uuid.UUID(finding_id))
        assert proposal is not None, 'no maintenance proposal row for the draft'
        assert proposal.target_type == 'procedural_entry'
        assert proposal.target_id == entry_id
        assert proposal.rule_name == DISTILLATION_RULE_NAME
        assert proposal.status == 'pending'


@pytest.mark.asyncio
async def test_case_submit_background_returns_pollable_job(
    api, http_client, metastore, fake_retain_factory
):
    """`background=true` returns 202 with a real job_id, pollable at
    GET /api/v1/ingestions/{job_id}; the case note id lands in the job's
    result.note_ids on completion, and a failure would be recorded on the job
    (not swallowed). Real ingest via fake_retain; case_of keeps it deterministic.
    """
    import asyncio

    api.memory.retain.side_effect = fake_retain_factory

    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'case_bg')
    entry = await api.procedural.create(
        _entry_dto(vault_id=vault_id, title='case-bg-target', verb='deploy', context='canary')
    )

    body = {
        'title': 'Background case submission',
        'trigger': 'fired a case without blocking the request',
        'outcome': 'success',
        'scope': 'project:bg',
        'scope_reasoning': 'Background test is scoped to the bg project.',
        'case_of': str(entry.id),
    }
    resp = await http_client.post('/api/v1/cases', params={'background': 'true'}, json=body)
    assert resp.status_code == 202, resp.text
    queued = resp.json()
    job_id = queued['job_id']
    assert queued['status'] == 'pending'

    # The background task runs off the request path — poll the job to completion.
    job = None
    for _ in range(60):
        jr = await http_client.get(f'/api/v1/ingestions/{job_id}')
        assert jr.status_code == 200, jr.text
        job = jr.json()
        if job['status'] in ('completed', 'failed'):
            break
        await asyncio.sleep(0.05)

    assert job is not None and job['status'] == 'completed', job
    assert job['result'] and job['result']['note_ids'], job


@pytest.mark.asyncio
async def test_case_list_returns_case_notes(api, http_client, metastore, fake_retain_factory):
    """GET /api/v1/cases returns only role='case' notes in the procedural
    system vault, and the outcome filter narrows the list."""
    api.memory.retain.side_effect = fake_retain_factory

    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'case_list')
    entry = await api.procedural.create(
        _entry_dto(vault_id=vault_id, title='case-list-target', verb='deploy', context='prod')
    )

    body = {
        'title': 'Listable case',
        'trigger': 'trigger text for list',
        'outcome': 'success',
        'scope': 'project:list',
        'scope_reasoning': 'List test is scoped to the list project.',
        'case_of': str(entry.id),
    }
    resp = await http_client.post('/api/v1/cases', json=body)
    assert resp.status_code == 200, resp.text
    note_id = resp.json()['note_id']

    # List all cases.
    list_resp = await http_client.get('/api/v1/cases')
    assert list_resp.status_code == 200, list_resp.text
    cases = list_resp.json()
    ids = [c['id'] for c in cases]
    assert str(note_id) in ids

    # Outcome filter matches.
    filtered = await http_client.get('/api/v1/cases', params={'outcome': 'success'})
    assert filtered.status_code == 200, filtered.text
    assert str(note_id) in [c['id'] for c in filtered.json()]

    # Outcome filter excludes.
    excluded = await http_client.get('/api/v1/cases', params={'outcome': 'failure'})
    assert excluded.status_code == 200, excluded.text
    assert str(note_id) not in [c['id'] for c in excluded.json()]


@pytest.mark.asyncio
async def test_case_get_returns_case_note_and_404s_non_cases(
    api, http_client, metastore, fake_retain_factory
):
    """GET /api/v1/cases/{note_id} returns the full case note; regular
    notes or random UUIDs return 404 so the case surface does not leak."""
    import uuid

    api.memory.retain.side_effect = fake_retain_factory

    async with metastore.session() as session:
        vault_id = await _create_vault(session, 'case_get')
        # A plain note in the same vault must NOT be reachable as a case.
        plain_note_id = uuid.uuid4()
        await session.execute(
            text(
                'INSERT INTO notes (id, content_hash, vault_id, original_text, title) '
                'VALUES (:id, :hash, :vault_id, :text, :title)'
            ),
            {
                'id': str(plain_note_id),
                'hash': 'plainhash',
                'vault_id': str(vault_id),
                'text': 'plain note',
                'title': 'Plain',
            },
        )
        await session.commit()

    entry = await api.procedural.create(
        _entry_dto(vault_id=vault_id, title='case-get-target', verb='deploy', context='prod')
    )

    body = {
        'title': 'Gettable case',
        'trigger': 'trigger text for get',
        'outcome': 'mixed',
        'scope': 'project:get',
        'scope_reasoning': 'Get test is scoped to the get project.',
        'case_of': str(entry.id),
    }
    resp = await http_client.post('/api/v1/cases', json=body)
    assert resp.status_code == 200, resp.text
    note_id = resp.json()['note_id']

    get_resp = await http_client.get(f'/api/v1/cases/{note_id}')
    assert get_resp.status_code == 200, get_resp.text
    got = get_resp.json()
    assert got['id'] == str(note_id)
    assert got['doc_metadata']['outcome'] == 'mixed'

    # Random UUID → 404.
    missing = await http_client.get(f'/api/v1/cases/{uuid.uuid4()}')
    assert missing.status_code == 404, missing.text

    # Plain note in a content vault → 404 (not role='case' in procedural vault).
    plain = await http_client.get(f'/api/v1/cases/{plain_note_id}')
    assert plain.status_code == 404, plain.text
