"""Tests for the V7 procedural-plane Hermes handlers.

The procedural plane ships 7 tools (create / upsert / get /
get_by_identity / update / deprecate / search) plus memex_case_submit
(cases are NOTES, not plane entries). These tests pin the Hermes-side
handler wiring:

- Each handler dispatches to the right ``api.procedural_*`` /
  ``api.case_submit`` method with the right kwargs.
- Vault_id is sourced from the session binding, NOT from the agent's
  args (the agent never names a vault directly).
- The kind/scope/verb/context matrix is enforced at the boundary
  (procedure REQUIRES verb+context; strategy REQUIRES verb and
  FORBIDS context).
- Missing required fields and bad kinds return ``tool_error`` JSON
  without touching the API.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from memex_common.procedural_schemas import (
    CaseAssignment,
    CaseSubmit,
    CaseSubmitResult,
    ProceduralEntryDTO,
    ProceduralSearchRequest,
    ProceduralSearchResponse,
    ProceduralSearchHit,
)

from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.tools import (
    ALL_SCHEMAS,
    HANDLERS,
    CASE_SUBMIT_SCHEMA,
    PROC_CREATE_SCHEMA,
    PROC_DEPRECATE_SCHEMA,
    PROC_GET_BY_IDENTITY_SCHEMA,
    PROC_GET_SCHEMA,
    PROC_SEARCH_SCHEMA,
    PROC_UPDATE_SCHEMA,
    PROC_UPSERT_SCHEMA,
    dispatch,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> HermesMemexConfig:
    return HermesMemexConfig()


@pytest.fixture
def vault_id():
    return uuid4()


def _fake_entry_dto(
    *,
    kind: str = 'procedure',
    scope: str = 'global',
    verb: str = 'rotate',
    context: str = 'creds',
    title: str = 'rotate API credentials',
) -> ProceduralEntryDTO:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return ProceduralEntryDTO(
        id=uuid4(),
        vault_id=uuid4(),
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        verb=verb,
        context=context,
        title=title,
        summary='How to rotate creds.',
        body='Step 1: ... Step 2: ...',
        trigger=None,
        tags=[],
        extra_metadata={},
        status='published',  # type: ignore[arg-type]
        origin='manual',
        supersedes_id=None,
        superseded_by_id=None,
        published_at=None,
        created_at=now,
        updated_at=now,
        sources=[],
        pins=[],
    )


# ---------------------------------------------------------------------------
# Registry: schemas + handlers
# ---------------------------------------------------------------------------


def test_all_seven_procedural_schemas_in_all_schemas():
    """The 7 procedural schemas are in ALL_SCHEMAS so the agent sees them."""
    proc_schemas = [s['name'] for s in ALL_SCHEMAS if s['name'].startswith('memex_procedural_')]
    assert proc_schemas == [
        'memex_procedural_create',
        'memex_procedural_upsert',
        'memex_procedural_get',
        'memex_procedural_get_by_identity',
        'memex_procedural_update',
        'memex_procedural_deprecate',
        'memex_procedural_search',
    ]


def test_case_submit_schema_registered():
    """memex_case_submit is in ALL_SCHEMAS with the CaseSubmit wire shape."""
    names = [s['name'] for s in ALL_SCHEMAS]
    assert names.count('memex_case_submit') == 1
    assert 'memex_procedural_briefing_cards' not in names
    params = CASE_SUBMIT_SCHEMA['parameters']
    assert sorted(params['required']) == ['outcome', 'title', 'trigger']
    assert set(params['properties']) == {
        'title',
        'trigger',
        'situation',
        'actions',
        'outcome',
        'lesson',
        'project_id',
        'case_of',
        'submitted_by',
        'tags',
    }
    assert params['properties']['outcome']['enum'] == ['success', 'failure', 'mixed']


def test_all_procedural_handlers_registered():
    """The 7 plane handlers + case_submit are wired in HANDLERS."""
    for name in (
        'memex_procedural_create',
        'memex_procedural_upsert',
        'memex_procedural_get',
        'memex_procedural_get_by_identity',
        'memex_procedural_update',
        'memex_procedural_deprecate',
        'memex_procedural_search',
        'memex_case_submit',
    ):
        assert name in HANDLERS, f'{name} missing from HANDLERS'
    assert 'memex_procedural_briefing_cards' not in HANDLERS


def test_schemas_have_required_fields():
    """Each schema is a valid OpenAI-style tool dict (name, description, parameters)."""
    for schema in (
        PROC_CREATE_SCHEMA,
        PROC_UPSERT_SCHEMA,
        PROC_GET_SCHEMA,
        PROC_GET_BY_IDENTITY_SCHEMA,
        PROC_UPDATE_SCHEMA,
        PROC_DEPRECATE_SCHEMA,
        PROC_SEARCH_SCHEMA,
        CASE_SUBMIT_SCHEMA,
    ):
        assert 'name' in schema
        assert 'description' in schema
        assert 'parameters' in schema
        params = schema['parameters']
        assert params['type'] == 'object'
        assert 'required' in params
        assert isinstance(params.get('properties'), dict)


# ---------------------------------------------------------------------------
# create — full payload, kind matrix
# ---------------------------------------------------------------------------


def test_create_calls_procedural_create_with_payload(config, vault_id):
    """A procedure-kind create hits api.procedural_create with an ProceduralEntryCreate."""
    api = Mock()
    expected = _fake_entry_dto(kind='procedure', scope='global', verb='rotate', context='creds')
    api.procedural_create = AsyncMock(return_value=expected)

    out = dispatch(
        'memex_procedural_create',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',
            'title': 'rotate API credentials',
            'summary': 'How to rotate creds.',
            'body': 'Step 1: ...',
            'trigger': 'an API credential is about to expire',
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )

    api.procedural_create.assert_awaited_once()
    call = api.procedural_create.await_args
    assert call is not None
    payload = call.args[0]
    assert payload.kind == 'procedure'
    assert payload.scope == 'global'
    assert payload.verb == 'rotate'
    assert payload.context == 'creds'
    assert payload.vault_id == vault_id  # session-bound, not from agent args
    # The returned DTO surfaces as JSON.
    data = json.loads(out)
    assert data['title'] == 'rotate API credentials'
    assert data['kind'] == 'procedure'


def test_create_case_kind_rejected(config, vault_id):
    """kind="case" no longer exists on the plane — cases go via memex_case_submit."""
    api = Mock()
    api.procedural_create = AsyncMock()

    out = dispatch(
        'memex_procedural_create',
        {
            'kind': 'case',
            'scope': 'global',
            'title': 'rotation failure',
            'summary': 'When API rotation fails',
            'trigger': 'rotation step 3 returns 500',
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'Invalid kind' in data['error']
    api.procedural_create.assert_not_awaited()


def test_create_procedure_kind_requires_verb_and_context(config, vault_id):
    """kind="procedure" without verb OR context → tool_error, no API call."""
    api = Mock()
    api.procedural_create = AsyncMock()

    out = dispatch(
        'memex_procedural_create',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',  # missing context
            'title': 't',
            'summary': 's',
            'trigger': 'creds about to expire',
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'verb and context' in data['error']
    api.procedural_create.assert_not_awaited()


def test_create_strategy_kind_forbids_context(config, vault_id):
    """kind="strategy" with a context → tool_error, no API call."""
    api = Mock()
    api.procedural_create = AsyncMock()

    out = dispatch(
        'memex_procedural_create',
        {
            'kind': 'strategy',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',  # WRONG — strategies anchor on scope+verb only
            'title': 't',
            'summary': 's',
            'trigger': 'when deciding how to rotate anything',
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'context' in data['error']
    api.procedural_create.assert_not_awaited()


def test_create_requires_trigger(config, vault_id):
    """trigger is the retrieval key — missing trigger → tool_error, no API call."""
    api = Mock()
    api.procedural_create = AsyncMock()

    out = dispatch(
        'memex_procedural_create',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',
            'title': 't',
            'summary': 's',
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'trigger' in data['error']
    api.procedural_create.assert_not_awaited()


def test_create_rejects_unknown_kind(config, vault_id):
    api = Mock()
    api.procedural_create = AsyncMock()

    out = dispatch(
        'memex_procedural_create',
        {'kind': 'oops', 'scope': 'global', 'title': 't', 'summary': 's'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'Invalid kind' in data['error']
    api.procedural_create.assert_not_awaited()


def test_create_missing_required_fields(config, vault_id):
    api = Mock()
    api.procedural_create = AsyncMock()

    out = dispatch(
        'memex_procedural_create',
        {'kind': 'procedure', 'scope': 'global'},  # missing title, summary, verb, context
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.procedural_create.assert_not_awaited()


def test_create_requires_vault_binding(config):
    """No vault bound to session → tool_error, no API call."""
    api = Mock()
    api.procedural_create = AsyncMock()

    out = dispatch(
        'memex_procedural_create',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',
            'title': 't',
            'summary': 's',
        },
        api=api,
        config=config,
        vault_id=None,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'vault' in data['error'].lower()
    api.procedural_create.assert_not_awaited()


# ---------------------------------------------------------------------------
# upsert — same shape as create, idempotent
# ---------------------------------------------------------------------------


def test_upsert_calls_procedural_upsert(config, vault_id):
    """upsert mirrors create's payload shape but routes to api.procedural_upsert."""
    api = Mock()
    expected = _fake_entry_dto()
    api.procedural_upsert = AsyncMock(return_value=expected)

    out = dispatch(
        'memex_procedural_upsert',
        {
            'kind': 'procedure',
            'scope': 'global',
            'verb': 'rotate',
            'context': 'creds',
            'title': 'rotate API credentials',
            'summary': 'How to rotate creds.',
            'trigger': 'an API credential is about to expire',
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )

    api.procedural_upsert.assert_awaited_once()
    call = api.procedural_upsert.await_args
    assert call is not None
    payload = call.args[0]
    assert payload.kind == 'procedure'
    assert payload.verb == 'rotate'
    data = json.loads(out)
    assert data['title'] == 'rotate API credentials'


# ---------------------------------------------------------------------------
# get — by UUID, vault-mismatch safe
# ---------------------------------------------------------------------------


def test_get_calls_procedural_get_with_uuid(config, vault_id):
    api = Mock()
    expected = _fake_entry_dto()
    api.procedural_get = AsyncMock(return_value=expected)

    entry_id = str(uuid4())
    out = dispatch(
        'memex_procedural_get',
        {'entry_id': entry_id},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call = api.procedural_get.await_args
    assert call is not None
    assert str(call.args[0]) == entry_id
    assert call.kwargs.get('vault_id') == vault_id
    data = json.loads(out)
    assert data['id'] == str(expected.id)


def test_get_rejects_invalid_uuid(config, vault_id):
    api = Mock()
    api.procedural_get = AsyncMock()

    out = dispatch(
        'memex_procedural_get',
        {'entry_id': 'not-a-uuid'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.procedural_get.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_by_identity — null on miss, returns DTO on hit
# ---------------------------------------------------------------------------


def test_get_by_identity_returns_null_on_miss(config, vault_id):
    """Unbound anchor returns None (NOT a 404 string). Cheap probe answer."""
    api = Mock()
    api.procedural_get_by_identity = AsyncMock(return_value=None)

    out = dispatch(
        'memex_procedural_get_by_identity',
        {'kind': 'procedure', 'scope': 'global', 'verb': 'rotate', 'context': 'creds'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data is None


def test_get_by_identity_returns_dto_on_hit(config, vault_id):
    api = Mock()
    expected = _fake_entry_dto()
    api.procedural_get_by_identity = AsyncMock(return_value=expected)

    out = dispatch(
        'memex_procedural_get_by_identity',
        {'kind': 'procedure', 'scope': 'global', 'verb': 'rotate', 'context': 'creds'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['id'] == str(expected.id)
    api.procedural_get_by_identity.assert_awaited_once()
    call = api.procedural_get_by_identity.await_args
    assert call is not None
    assert call.kwargs['kind'] == 'procedure'
    assert call.kwargs['scope'] == 'global'
    assert call.kwargs['verb'] == 'rotate'
    assert call.kwargs['context'] == 'creds'
    assert call.kwargs['vault_id'] == vault_id


def test_get_by_identity_requires_verb(config, vault_id):
    """verb is required for both kinds — missing verb → tool_error, no API call."""
    api = Mock()
    api.procedural_get_by_identity = AsyncMock()

    out = dispatch(
        'memex_procedural_get_by_identity',
        {'kind': 'procedure', 'scope': 'global', 'context': 'creds'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'verb' in data['error']
    api.procedural_get_by_identity.assert_not_awaited()


def test_get_by_identity_procedure_requires_context(config, vault_id):
    api = Mock()
    api.procedural_get_by_identity = AsyncMock()

    out = dispatch(
        'memex_procedural_get_by_identity',
        {'kind': 'procedure', 'scope': 'global', 'verb': 'rotate'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'context' in data['error']
    api.procedural_get_by_identity.assert_not_awaited()


def test_get_by_identity_strategy_forbids_context(config, vault_id):
    """Strategy anchors on (scope, verb) only — context supplied → tool_error."""
    api = Mock()
    api.procedural_get_by_identity = AsyncMock()

    out = dispatch(
        'memex_procedural_get_by_identity',
        {'kind': 'strategy', 'scope': 'global', 'verb': 'rotate', 'context': 'creds'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'omit context' in data['error']
    api.procedural_get_by_identity.assert_not_awaited()


# ---------------------------------------------------------------------------
# update — at least one field, by UUID
# ---------------------------------------------------------------------------


def test_update_requires_at_least_one_field(config, vault_id):
    api = Mock()
    api.procedural_update = AsyncMock()

    out = dispatch(
        'memex_procedural_update',
        {'entry_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.procedural_update.assert_not_awaited()


def test_update_calls_procedural_update_with_partial_payload(config, vault_id):
    api = Mock()
    expected = _fake_entry_dto(title='updated title')
    api.procedural_update = AsyncMock(return_value=expected)

    entry_id = str(uuid4())
    out = dispatch(
        'memex_procedural_update',
        {'entry_id': entry_id, 'title': 'updated title', 'summary': 'updated body'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    api.procedural_update.assert_awaited_once()
    call = api.procedural_update.await_args
    assert call is not None
    assert str(call.args[0]) == entry_id
    payload = call.args[1]
    assert payload.title == 'updated title'
    assert payload.summary == 'updated body'
    assert call.kwargs.get('vault_id') == vault_id
    data = json.loads(out)
    assert data['title'] == 'updated title'


# ---------------------------------------------------------------------------
# deprecate — supersession edge
# ---------------------------------------------------------------------------


def test_deprecate_calls_procedural_deprecate(config, vault_id):
    api = Mock()
    expected = _fake_entry_dto()
    expected_dto = expected.model_copy(update={'status': 'deprecated'})
    api.procedural_deprecate = AsyncMock(return_value=expected_dto)

    entry_id = str(uuid4())
    superseded = str(uuid4())
    out = dispatch(
        'memex_procedural_deprecate',
        {'entry_id': entry_id, 'superseded_by_id': superseded},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call = api.procedural_deprecate.await_args
    assert call is not None
    assert str(call.args[0]) == entry_id
    assert str(call.kwargs['superseded_by_id']) == superseded
    assert call.kwargs['vault_id'] == vault_id
    data = json.loads(out)
    assert data['status'] == 'deprecated'


def test_deprecate_rejects_invalid_superseded_uuid(config, vault_id):
    api = Mock()
    api.procedural_deprecate = AsyncMock()

    out = dispatch(
        'memex_procedural_deprecate',
        {'entry_id': str(uuid4()), 'superseded_by_id': 'not-a-uuid'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.procedural_deprecate.assert_not_awaited()


# ---------------------------------------------------------------------------
# search — query required, optional filters
# ---------------------------------------------------------------------------


def test_search_calls_procedural_search_with_request(config, vault_id):
    """The handler builds an ProceduralSearchRequest and threads vault_id."""
    api = Mock()
    expected_response = ProceduralSearchResponse(
        hits=[
            ProceduralSearchHit(
                entry=_fake_entry_dto(),
                score=0.9,
                bm25_rank=0,
                vector_rank=None,
                matched_via='bm25',
                pin_position=None,
            )
        ],
        total=1,
        truncated=False,
        took_ms=12.3,
    )
    api.procedural_search = AsyncMock(return_value=expected_response)

    out = dispatch(
        'memex_procedural_search',
        {'query': 'rotate creds', 'kind': 'procedure', 'top_k': 5},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    api.procedural_search.assert_awaited_once()
    call = api.procedural_search.await_args
    assert call is not None
    request: ProceduralSearchRequest = call.args[0]
    assert request.query == 'rotate creds'
    assert request.kind == 'procedure'
    assert request.limit == 5
    assert request.vault_id == vault_id
    data = json.loads(out)
    assert 'hits' in data
    assert data['total'] == 1


def test_search_rejects_unknown_kind(config, vault_id):
    api = Mock()
    api.procedural_search = AsyncMock()

    out = dispatch(
        'memex_procedural_search',
        {'query': 'q', 'kind': 'oops'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.procedural_search.assert_not_awaited()


# ---------------------------------------------------------------------------
# case_submit — cases are notes; assignment envelope returned verbatim
# ---------------------------------------------------------------------------


def _fake_case_submit_result(**assignment_kwargs) -> CaseSubmitResult:
    return CaseSubmitResult(
        note_id=uuid4(),
        vault_id=uuid4(),
        assignment=CaseAssignment(**{'mode': 'skipped', **assignment_kwargs}),
    )


def test_case_submit_builds_payload_and_dispatches(config, vault_id):
    """The handler builds a CaseSubmit DTO and routes it to api.case_submit."""
    api = Mock()
    expected = _fake_case_submit_result(mode='explicit')
    api.case_submit = AsyncMock(return_value=expected)

    case_of = uuid4()
    out = dispatch(
        'memex_case_submit',
        {
            'title': 'rotation step 3 returned 500',
            'trigger': 'rotating creds for service X',
            'situation': 'cert had expired mid-rotation',
            'actions': ['re-issued cert', 'retried rotation'],
            'outcome': 'success',
            'lesson': 'check cert expiry before rotating',
            'project_id': 'proj-alpha',
            'case_of': str(case_of),
            'submitted_by': 'hermes',
            'tags': ['rotation'],
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )

    api.case_submit.assert_awaited_once()
    call = api.case_submit.await_args
    assert call is not None
    payload = call.args[0]
    assert isinstance(payload, CaseSubmit)
    assert payload.title == 'rotation step 3 returned 500'
    assert payload.trigger == 'rotating creds for service X'
    assert payload.situation == 'cert had expired mid-rotation'
    assert payload.actions == ['re-issued cert', 'retried rotation']
    assert payload.outcome == 'success'
    assert payload.lesson == 'check cert expiry before rotating'
    assert payload.project_id == 'proj-alpha'
    assert payload.case_of == case_of  # UUID-parsed, not a raw string
    assert isinstance(payload.case_of, UUID)
    assert payload.submitted_by == 'hermes'
    assert payload.tags == ['rotation']

    data = json.loads(out)
    assert data['note_id'] == str(expected.note_id)
    assert data['assignment']['mode'] == 'explicit'


def test_case_submit_minimal_payload_omits_optional_fields(config, vault_id):
    """Only title/trigger/outcome supplied → DTO defaults, no None kwargs."""
    api = Mock()
    api.case_submit = AsyncMock(return_value=_fake_case_submit_result())

    out = dispatch(
        'memex_case_submit',
        {'title': 't', 'trigger': 'tr', 'outcome': 'failure'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call = api.case_submit.await_args
    assert call is not None
    payload = call.args[0]
    assert payload.outcome == 'failure'
    assert payload.situation == ''
    assert payload.actions == []
    assert payload.lesson == ''
    assert payload.project_id is None
    assert payload.case_of is None
    assert payload.submitted_by is None
    assert payload.tags == []
    data = json.loads(out)
    assert data['assignment']['mode'] == 'skipped'


def test_case_submit_rejects_invalid_case_of_uuid(config, vault_id):
    api = Mock()
    api.case_submit = AsyncMock()

    out = dispatch(
        'memex_case_submit',
        {'title': 't', 'trigger': 'tr', 'outcome': 'mixed', 'case_of': 'not-a-uuid'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'case_of' in data['error']
    api.case_submit.assert_not_awaited()


def test_case_submit_rejects_invalid_outcome(config, vault_id):
    api = Mock()
    api.case_submit = AsyncMock()

    out = dispatch(
        'memex_case_submit',
        {'title': 't', 'trigger': 'tr', 'outcome': 'oops'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.case_submit.assert_not_awaited()


def test_case_submit_missing_required_fields(config, vault_id):
    api = Mock()
    api.case_submit = AsyncMock()

    out = dispatch(
        'memex_case_submit',
        {'title': 't'},  # missing trigger + outcome
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.case_submit.assert_not_awaited()
