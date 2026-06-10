"""Tests for the V7 procedural-plane Hermes handlers.

The procedural plane ships 8 tools (create / upsert / get /
get_by_identity / update / deprecate / search / briefing_cards). These
tests pin the Hermes-side handler wiring:

- Each handler dispatches to the right ``api.procedural_*`` method
  with the right kwargs.
- Vault_id is sourced from the session binding, NOT from the agent's
  args (the agent never names a vault directly).
- The kind/scope/verb/context matrix is enforced at the boundary
  (case MUST omit verb+context; procedure+strategy REQUIRE both).
- Missing required fields and bad kinds return ``tool_error`` JSON
  without touching the API.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from memex_common.experiential_schemas import (
    ExperientialBriefingCards,
    ExperientialEntryDTO,
    ExperientialSearchRequest,
    ExperientialSearchResponse,
    ExperientialBriefingCard,
    ExperientialSearchHit,
)

from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.tools import (
    ALL_SCHEMAS,
    HANDLERS,
    PROC_BRIEFING_CARDS_SCHEMA,
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
) -> ExperientialEntryDTO:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return ExperientialEntryDTO(
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


def test_all_eight_procedural_schemas_in_all_schemas():
    """The 8 procedural schemas are in ALL_SCHEMAS so the agent sees them."""
    proc_schemas = [s['name'] for s in ALL_SCHEMAS if s['name'].startswith('memex_procedural_')]
    assert proc_schemas == [
        'memex_procedural_create',
        'memex_procedural_upsert',
        'memex_procedural_get',
        'memex_procedural_get_by_identity',
        'memex_procedural_update',
        'memex_procedural_deprecate',
        'memex_procedural_search',
        'memex_procedural_briefing_cards',
    ]


def test_all_eight_procedural_handlers_registered():
    """The 8 procedural handlers are wired in HANDLERS so dispatch() routes to them."""
    for name in (
        'memex_procedural_create',
        'memex_procedural_upsert',
        'memex_procedural_get',
        'memex_procedural_get_by_identity',
        'memex_procedural_update',
        'memex_procedural_deprecate',
        'memex_procedural_search',
        'memex_procedural_briefing_cards',
    ):
        assert name in HANDLERS, f'{name} missing from HANDLERS'


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
        PROC_BRIEFING_CARDS_SCHEMA,
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
    """A procedure-kind create hits api.procedural_create with an ExperientialEntryCreate."""
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


def test_create_case_kind_omits_verb_and_context(config, vault_id):
    """kind="case" MUST omit verb and context. Handler rejects payloads that violate this."""
    api = Mock()
    api.procedural_create = AsyncMock()

    out = dispatch(
        'memex_procedural_create',
        {
            'kind': 'case',
            'scope': 'global',
            'verb': 'rotate',  # WRONG — case has no verb
            'context': 'creds',
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
    assert 'kind="case"' in data['error']
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
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'REQUIRE' in data['error']
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
    """The handler builds an ExperientialSearchRequest and threads vault_id."""
    api = Mock()
    expected_response = ExperientialSearchResponse(
        hits=[
            ExperientialSearchHit(
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
    request: ExperientialSearchRequest = call.args[0]
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
# briefing_cards — required context_keys list
# ---------------------------------------------------------------------------


def test_briefing_cards_requires_non_empty_keys(config, vault_id):
    api = Mock()
    api.procedural_briefing_cards = AsyncMock()

    out = dispatch(
        'memex_procedural_briefing_cards',
        {'context_keys': []},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.procedural_briefing_cards.assert_not_awaited()


def test_briefing_cards_calls_procedural_briefing_cards(config, vault_id):
    api = Mock()
    entry = _fake_entry_dto()
    expected_response = ExperientialBriefingCards(
        cards=[
            ExperientialBriefingCard(
                entry=entry,
                pin_position=0,
                context_key='global',
            )
        ],
        context_keys=['global'],
        total_pinned=1,
    )
    api.procedural_briefing_cards = AsyncMock(return_value=expected_response)

    out = dispatch(
        'memex_procedural_briefing_cards',
        {'context_keys': ['global'], 'limit_per_context': 3},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    api.procedural_briefing_cards.assert_awaited_once()
    call = api.procedural_briefing_cards.await_args
    assert call is not None
    assert call.args[0] == ['global']
    assert call.kwargs['limit_per_context'] == 3
    data = json.loads(out)
    assert data['total_pinned'] == 1
    assert len(data['cards']) == 1
