"""Tests for the procedural-plane Hermes handlers.

The procedural plane exposes 3 READ tools on the agent surface (get /
get_by_identity / search) plus memex_case_submit (cases are NOTES, not
plane entries). The WRITE tools (create / upsert / update / deprecate)
are deliberately NOT on the agent surface: procedures/strategies are
DERIVED from cases, so the agent's only procedural write is
``case_submit``. These tests pin the Hermes-side handler wiring:

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
    PROC_GET_BY_IDENTITY_SCHEMA,
    PROC_GET_SCHEMA,
    PROC_SEARCH_SCHEMA,
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


def test_procedural_read_schemas_in_all_schemas():
    """Only the 3 procedural READ schemas are in ALL_SCHEMAS — the agent
    sees get / get_by_identity / search but NO write tools (procedures are
    derived from cases; the agent writes via case_submit)."""
    proc_schemas = [s['name'] for s in ALL_SCHEMAS if s['name'].startswith('memex_procedural_')]
    assert proc_schemas == [
        'memex_procedural_get',
        'memex_procedural_get_by_identity',
        'memex_procedural_search',
    ]


def test_procedural_write_schemas_absent_from_all_schemas():
    """The write tools must NOT be exposed to the agent."""
    names = {s['name'] for s in ALL_SCHEMAS}
    for forbidden in (
        'memex_procedural_create',
        'memex_procedural_upsert',
        'memex_procedural_update',
        'memex_procedural_deprecate',
    ):
        assert forbidden not in names, f'{forbidden} must not be on the Hermes agent surface'


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
    """The 3 plane READ handlers + case_submit are wired in HANDLERS."""
    for name in (
        'memex_procedural_get',
        'memex_procedural_get_by_identity',
        'memex_procedural_search',
        'memex_case_submit',
    ):
        assert name in HANDLERS, f'{name} missing from HANDLERS'
    assert 'memex_procedural_briefing_cards' not in HANDLERS
    for forbidden in (
        'memex_procedural_create',
        'memex_procedural_upsert',
        'memex_procedural_update',
        'memex_procedural_deprecate',
    ):
        assert forbidden not in HANDLERS, f'{forbidden} must not be wired'


def test_schemas_have_required_fields():
    """Each schema is a valid OpenAI-style tool dict (name, description, parameters)."""
    for schema in (
        PROC_GET_SCHEMA,
        PROC_GET_BY_IDENTITY_SCHEMA,
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
