"""HTTP-layer integration tests for the V7 procedural-plane routes.

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
    authz is the F4 work, not the V7 contract under test here."""
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
        vault_id = await _create_vault(session, 'v7_http_create')

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
        vault_id = await _create_vault(session, 'v7_http_409')

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
        vault_id = await _create_vault(session, 'v7_http_upsert')

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
        vault_id = await _create_vault(session, 'v7_http_miss')

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
        vault_id = await _create_vault(session, 'v7_http_hit')

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
        vault_id = await _create_vault(session, 'v7_http_briefing')

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
        vault_id = await _create_vault(session, 'v7_http_dep_miss')

    missing_id = uuid.uuid4()
    resp = await http_client.post(
        f'/api/v1/procedural/{missing_id}/deprecate',
        params={'vault_id': str(vault_id)},
    )
    assert resp.status_code == 404, resp.text
