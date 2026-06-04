"""HTTP-route integration tests for the external lint-proposal ingress.

Drives the REAL routes (ASGITransport) against the REAL ``MemexAPI`` +
Postgres fixture — submission batch semantics, catalogue discoverability,
preview gating, the global-finding authorization fence, and a destructive
action executed end-to-end through ``/resolve``.

``MEMEX_LINT_ALLOW_UNATTENDED_APPLY=1`` is set where a canned action
executes: the suite runs with auth disabled, and the attended-mode fence
exists precisely to block that combination outside trusted test/CI runs.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from memex_core.server import app
from memex_core.server.common import get_api


@pytest_asyncio.fixture
async def http(api) -> AsyncGenerator[AsyncClient, None]:
    app.dependency_overrides[get_api] = lambda: api
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_api, None)


def _proposal(vault_id: str, *, rule_name: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        'vault_id': vault_id,
        'rule_name': rule_name,
        'lint_type': 'quality',
        'target_type': 'memory_unit',
        'target_id': str(uuid4()),
        'description': f'route-contract finding {uuid4()}',
        'suggested_action': 'review in the cockpit',
    }
    body.update(overrides)
    return body


async def _create_vault(http: AsyncClient) -> str:
    resp = await http.post('/api/v1/vaults', json={'name': f'routes-{uuid4().hex[:8]}'})
    assert resp.status_code in (200, 201), resp.text
    return str(resp.json()['id'])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_partial_success_statuses(http: AsyncClient):
    vault_id = await _create_vault(http)
    good = _proposal(vault_id, rule_name='route-contract-good')
    reserved = _proposal(vault_id, rule_name='composite_deprioritize_candidate')
    missing_vault = _proposal(vault_id, rule_name='route-contract-novault')
    missing_vault.pop('vault_id')
    unknown_vault = _proposal('definitely-not-a-vault', rule_name='route-contract-badvault')

    resp = await http.post(
        '/api/v1/lint/proposals',
        json={'proposals': [good, reserved, missing_vault, unknown_vault, good]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    statuses = [item['status'] for item in body['results']]
    assert statuses == ['created', 'rejected', 'rejected', 'rejected', 'deduplicated']
    assert body['created'] == 1 and body['rejected'] == 3 and body['deduplicated'] == 1
    assert 'reserved' in body['results'][1]['detail']
    assert 'vault_id is required' in body['results'][2]['detail']
    assert 'unknown vault' in body['results'][3]['detail']
    assert body['results'][4]['finding_id'] == body['results'][0]['finding_id']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_envelope_rejects_oversized_batch(http: AsyncClient, api):
    cap = api.config.server.memory.lint.external_proposals.max_batch
    vault_id = str(uuid4())
    items = [_proposal(vault_id, rule_name='cap-check') for _ in range(cap + 1)]
    resp = await http.post('/api/v1/lint/proposals', json={'proposals': items})
    assert resp.status_code == 400
    assert 'max_batch' in resp.json()['detail']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_actions_catalogue_route(http: AsyncClient):
    resp = await http.get('/api/v1/lint/actions')
    assert resp.status_code == 200, resp.text
    actions = {a['id']: a for a in resp.json()['actions']}
    assert len(actions) >= 15
    assert actions['collapse_into_new_entity']['params_schema']['required'] == [
        'new_canonical_name',
        'member_ids',
    ]
    assert actions['delete_note']['reversible'] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preview_route_validates_and_previews(http: AsyncClient):
    vault_id = await _create_vault(http)
    submit = await http.post(
        '/api/v1/lint/proposals',
        json=_proposal(
            vault_id,
            rule_name='route-contract-preview',
            lint_type='routing',
            target_type='note',
        ),
    )
    finding_id = submit.json()['results'][0]['finding_id']

    unknown = await http.post(
        f'/api/v1/lint/findings/{finding_id}/preview', json={'action': 'nope'}
    )
    assert unknown.status_code == 400

    mismatch = await http.post(
        f'/api/v1/lint/findings/{finding_id}/preview', json={'action': 'deprioritize_unit'}
    )
    assert mismatch.status_code == 400
    assert 'does not apply' in mismatch.json()['detail']

    happy = await http.post(
        f'/api/v1/lint/findings/{finding_id}/preview',
        json={'action': 'route_note_to_vault', 'params': {'target_vault_id': vault_id}},
    )
    assert happy.status_code == 200, happy.text
    assert 'migrate' in happy.json()['preview']
    assert happy.json()['reversible'] is True


async def _insert_global_entity_finding(api, *, with_scope: bool, vault_id: str | None) -> str:
    """A NULL-vault entity finding, with or without vaults_affected evidence."""
    finding_id = str(uuid4())
    evidence: dict[str, Any] = {'cluster_members': [str(uuid4()), str(uuid4())]}
    if with_scope and vault_id:
        evidence['vaults_affected'] = [vault_id]
    import json

    async with api.metastore.session() as s:
        await s.execute(
            text(
                'INSERT INTO maintenance_proposals '
                '(id, vault_id, lint_type, target_type, target_id, rule_name, evidence, '
                ' suggested_action, status, source) VALUES '
                "(:id, NULL, 'structural', 'entity', :target_id, :rule_name, "
                " CAST(:evidence AS jsonb), 'merge', 'pending', 'rule')"
            ),
            {
                'id': finding_id,
                'target_id': evidence['cluster_members'][0],
                'rule_name': f'global-entity-{uuid4().hex[:6]}',
                'evidence': json.dumps(evidence),
            },
        )
        await s.commit()
    return finding_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_global_finding_action_refused_without_scope(http: AsyncClient, api, monkeypatch):
    """The no-fail-open gate: a NULL-vault finding with no vaults_affected
    evidence cannot execute (or preview) a mutating catalogue action."""
    monkeypatch.setenv('MEMEX_LINT_ALLOW_UNATTENDED_APPLY', '1')
    finding_id = await _insert_global_entity_finding(api, with_scope=False, vault_id=None)
    members = [str(uuid4()), str(uuid4())]
    params = {'winner_id': members[0], 'member_ids': members}

    resolve = await http.post(
        f'/api/v1/lint/findings/{finding_id}/resolve',
        json={'action': 'merge_entities', 'params': params},
    )
    assert resolve.status_code == 400, resolve.text
    assert 'vaults_affected' in resolve.json()['detail']

    preview = await http.post(
        f'/api/v1/lint/findings/{finding_id}/preview',
        json={'action': 'merge_entities', 'params': params},
    )
    assert preview.status_code == 400
    assert 'vaults_affected' in preview.json()['detail']

    # no_op stays allowed — it mutates nothing beyond the status flip that
    # global findings already permit.
    noop = await http.post(f'/api/v1/lint/findings/{finding_id}/resolve', json={'action': 'no_op'})
    assert noop.status_code == 200, noop.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_destructive_action_executes_through_resolve(http: AsyncClient, api, monkeypatch):
    """kv_delete end-to-end: submit → resolve with the catalogue action →
    followup stamped with prior/applied state and the KV entry gone."""
    monkeypatch.setenv('MEMEX_LINT_ALLOW_UNATTENDED_APPLY', '1')
    vault_id = await _create_vault(http)
    key = f'global:route-contract:{uuid4().hex[:8]}'
    await api.kv_put(key, 'doomed-value')
    assert await api.kv_get(key) is not None

    submit = await http.post(
        '/api/v1/lint/proposals',
        json=_proposal(
            vault_id,
            rule_name='route-contract-kv',
            lint_type='governance',
            target_type='kv',
            target_id=key,
            proposed_action={'action_name': 'kv_delete', 'params': {}},
        ),
    )
    item = submit.json()['results'][0]
    assert item['status'] == 'created', submit.text

    resolve = await http.post(
        f'/api/v1/lint/findings/{item["finding_id"]}/resolve',
        json={'action': 'kv_delete', 'note': 'route-contract: confirmed stale'},
    )
    assert resolve.status_code == 200, resolve.text
    followup = resolve.json()['resolution']['followup']
    assert followup['action'] == 'kv_delete'
    assert followup['reversible'] is False
    assert followup['applied_state']['deleted'] is True
    assert await api.kv_get(key) is None

    reverse = await http.post(f'/api/v1/lint/findings/{item["finding_id"]}/reverse')
    assert reverse.status_code == 409
    assert reverse.json()['detail']['reason'] == 'forward_only'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submitted_suggestion_validated_at_the_door(http: AsyncClient):
    vault_id = await _create_vault(http)
    bad_action = _proposal(
        vault_id,
        rule_name='route-contract-deadsuggestion',
        proposed_action={'action_name': 'not_a_real_action', 'params': {}},
    )
    bad_params = _proposal(
        vault_id,
        rule_name='route-contract-badparams',
        lint_type='routing',
        target_type='note',
        proposed_action={'action_name': 'route_note_to_vault', 'params': {}},
    )
    resp = await http.post('/api/v1/lint/proposals', json={'proposals': [bad_action, bad_params]})
    statuses = [item['status'] for item in resp.json()['results']]
    assert statuses == ['rejected', 'rejected']
    assert 'unknown' in resp.json()['results'][0]['detail']
    assert 'params invalid' in resp.json()['results'][1]['detail']
