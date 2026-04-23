"""Tests for Memex tool schemas + handlers.

The handlers wrap ``RemoteMemexAPI`` methods; we mock the API and verify
dispatch, arg handling, and error paths. Fixtures build real ``memex_common``
DTOs so a schema drift in the client is caught here, not in production.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from memex_common.schemas import (
    BlockSummaryDTO,
    EntityDTO,
    IngestResponse,
    MemoryUnitDTO,
    NoteSearchResult,
    SurveyFact,
    SurveyResponse,
    SurveyTopic,
)

from memex_hermes_plugin.memex.config import HermesMemexConfig
from memex_hermes_plugin.memex.tools import ALL_SCHEMAS, dispatch


@pytest.fixture
def config() -> HermesMemexConfig:
    return HermesMemexConfig()


@pytest.fixture
def vault_id():
    return uuid4()


def _fake_memory_unit(text: str = 'a fact') -> MemoryUnitDTO:
    # ``FactTypes`` accepts 'world', 'event', or 'observation' only.
    return MemoryUnitDTO(
        id=uuid4(),
        note_id=uuid4(),
        text=text,
        fact_type='world',
        status='active',
    )


def _fake_note_result(name: str = 'note') -> NoteSearchResult:
    return NoteSearchResult(
        note_id=uuid4(),
        metadata={'name': name, 'title': name, 'description': 'd', 'tags': ['x']},
        summaries=[BlockSummaryDTO(topic='Intro about note', key_points=['pt1'])],
        score=0.9,
        note_status='active',
        vault_name='v',
    )


def _fake_entity(name: str = 'Rust') -> EntityDTO:
    return EntityDTO(id=uuid4(), name=name, mention_count=5)


def test_all_schemas_have_required_fields():
    names = {s['name'] for s in ALL_SCHEMAS}
    expected_stream_1 = {
        'memex_recall',
        'memex_retrieve_notes',
        'memex_survey',
        'memex_retain',
        'memex_list_entities',
        'memex_get_entity_mentions',
        'memex_get_entity_cooccurrences',
    }
    expected_stream_4 = {
        'memex_set_note_status',
        'memex_update_user_notes',
        'memex_rename_note',
        'memex_get_template',
        'memex_list_templates',
        'memex_register_template',
    }
    assert expected_stream_1.issubset(names)
    assert expected_stream_4.issubset(names)
    for s in ALL_SCHEMAS:
        assert 'description' in s
        assert s['parameters']['type'] == 'object'


def test_tool_descriptions_are_neutral():
    """Descriptions describe what the tool does, not when to combine it.

    Routing (parallel/sequential dispatch) lives in the system prompt block,
    not per-tool — matches MCP's convention and keeps tool schemas compact.
    """
    for schema in ALL_SCHEMAS:
        desc = schema['description'].lower()
        assert 'in parallel' not in desc, (
            f'{schema["name"]} leaked a "parallel" hint into its description; '
            'routing belongs in the system prompt block (briefing.py).'
        )


def test_recall_returns_serialized_results(config, vault_id):
    api = Mock()
    api.search = AsyncMock(return_value=[_fake_memory_unit('X is Y')])
    out = dispatch('memex_recall', {'query': 'X'}, api=api, config=config, vault_id=vault_id)
    data = json.loads(out)
    assert data['count'] == 1
    assert data['results'][0]['text'] == 'X is Y'
    api.search.assert_awaited_once()


def test_recall_missing_query_returns_error(config, vault_id):
    api = Mock()
    out = dispatch('memex_recall', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)


def test_retrieve_notes(config, vault_id):
    api = Mock()
    api.search_notes = AsyncMock(return_value=[_fake_note_result('doc')])
    out = dispatch(
        'memex_retrieve_notes',
        {'query': 'doc'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['count'] == 1
    result = data['results'][0]
    assert result['name'] == 'doc'
    # BlockSummaryDTO fields surface.
    assert result['summaries'][0]['topic'] == 'Intro about note'
    assert result['summaries'][0]['key_points'] == ['pt1']


def test_survey(config, vault_id):
    api = Mock()
    note_id = uuid4()
    topic = SurveyTopic(
        note_id=note_id,
        title='Source doc',
        fact_count=1,
        facts=[
            SurveyFact(id=uuid4(), text='fact a', fact_type='world', score=0.9),
        ],
    )
    response = SurveyResponse(
        query='broad',
        sub_queries=['sub1'],
        topics=[topic],
        total_notes=1,
        total_facts=1,
        truncated=False,
    )
    api.survey = AsyncMock(return_value=response)
    out = dispatch('memex_survey', {'query': 'broad'}, api=api, config=config, vault_id=vault_id)
    data = json.loads(out)
    assert data['total_facts'] == 1
    assert data['sub_queries'] == ['sub1']
    assert data['topics'][0]['title'] == 'Source doc'
    assert data['topics'][0]['facts'][0]['text'] == 'fact a'
    assert data['topics'][0]['facts'][0]['fact_type'] == 'world'


def test_retain_ingests_note(config, vault_id):
    api = Mock()
    api.ingest = AsyncMock(return_value=IngestResponse(status='success', note_id=str(uuid4())))
    out = dispatch(
        'memex_retain',
        {
            'name': 'decision',
            'description': 'short',
            'content': 'user prefers X',
            'note_key': 'hermes:session:abc',
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['status'] == 'success'
    api.ingest.assert_awaited_once()
    dto = api.ingest.call_args.args[0]
    # vault_id and author propagate to the DTO.
    assert str(dto.vault_id) == str(vault_id)
    assert dto.author == 'hermes'
    assert dto.note_key == 'hermes:session:abc'


def test_retain_missing_required_params(config, vault_id):
    api = Mock()
    out = dispatch(
        'memex_retain',
        {'name': 'x'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)


def test_list_entities(config, vault_id):
    api = Mock()
    api.search_entities = AsyncMock(return_value=[_fake_entity('Rust')])
    out = dispatch(
        'memex_list_entities',
        {'query': 'Rust'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['results'][0]['name'] == 'Rust'
    assert data['results'][0]['mention_count'] == 5


def test_list_entities_missing_query(config, vault_id):
    api = Mock()
    out = dispatch('memex_list_entities', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)


def test_get_entity_mentions(config, vault_id):
    from datetime import datetime, timezone

    from memex_common.schemas import NoteDTO

    api = Mock()
    note_uuid = uuid4()
    note_vault = uuid4()
    note = NoteDTO(
        id=note_uuid,
        title='a note',
        vault_id=note_vault,
        created_at=datetime.now(timezone.utc),
    )
    api.get_entity_mentions = AsyncMock(
        return_value=[{'unit': _fake_memory_unit('mention'), 'note': note}]
    )
    out = dispatch(
        'memex_get_entity_mentions',
        {'entity_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['results'][0]['unit']['text'] == 'mention'
    assert data['results'][0]['note_id'] == str(note_uuid)


def test_get_entity_mentions_missing_entity_id(config, vault_id):
    api = Mock()
    out = dispatch('memex_get_entity_mentions', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)


def test_get_entity_cooccurrences(config, vault_id):
    """Matches the real server response shape in ``entities.py``."""
    api = Mock()
    queried = uuid4()
    other = uuid4()
    api.get_entity_cooccurrences = AsyncMock(
        return_value=[
            {
                'entity_id_1': queried,
                'entity_id_2': other,
                'entity_1_name': 'Rust',
                'entity_1_type': 'tool',
                'entity_2_name': 'Python',
                'entity_2_type': 'tool',
                'cooccurrence_count': 7,
                'vault_id': uuid4(),
            }
        ]
    )
    out = dispatch(
        'memex_get_entity_cooccurrences',
        {'entity_id': str(queried)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    item = json.loads(out)['results'][0]
    assert item['count'] == 7
    assert item['name'] == 'Python'
    assert item['type'] == 'tool'
    assert item['entity_id'] == str(other)


def test_get_entity_cooccurrences_pivots_when_queried_is_entity_2(config, vault_id):
    """If the queried id is on side 2, still pivots to the correct counterpart."""
    api = Mock()
    queried = uuid4()
    other = uuid4()
    api.get_entity_cooccurrences = AsyncMock(
        return_value=[
            {
                'entity_id_1': other,
                'entity_id_2': queried,
                'entity_1_name': 'Gopher',
                'entity_1_type': 'mascot',
                'entity_2_name': 'Rust',
                'entity_2_type': 'tool',
                'cooccurrence_count': 3,
                'vault_id': uuid4(),
            }
        ]
    )
    out = dispatch(
        'memex_get_entity_cooccurrences',
        {'entity_id': str(queried)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    item = json.loads(out)['results'][0]
    assert item['name'] == 'Gopher'
    assert item['entity_id'] == str(other)


def test_get_entity_cooccurrences_missing_entity_id(config, vault_id):
    api = Mock()
    out = dispatch('memex_get_entity_cooccurrences', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)


def test_unknown_tool(config, vault_id):
    api = Mock()
    out = dispatch('memex_nonexistent', {}, api=api, config=config, vault_id=vault_id)
    assert 'Unknown tool' in json.loads(out)['error']


def test_recall_forwards_api_errors(config, vault_id):
    api = Mock()
    api.search = AsyncMock(side_effect=RuntimeError('backend down'))
    out = dispatch('memex_recall', {'query': 'x'}, api=api, config=config, vault_id=vault_id)
    assert 'backend down' in json.loads(out)['error']


# ---------------------------------------------------------------------------
# Stream 1: vault-scoping helper + bug-fix regressions
# ---------------------------------------------------------------------------

import httpx  # noqa: E402

from memex_common.schemas import VaultDTO  # noqa: E402

from memex_hermes_plugin.memex.tools import (  # noqa: E402
    GET_ENTITY_COOCCURRENCES_SCHEMA,
    GET_ENTITY_MENTIONS_SCHEMA,
    LIST_ENTITIES_SCHEMA,
    RECALL_SCHEMA,
    RETRIEVE_NOTES_SCHEMA,
    SURVEY_SCHEMA,
    VaultResolutionError,
    _resolve_vault_ids,
)


def _fake_vault(vault_id=None, name: str = 'v') -> VaultDTO:
    return VaultDTO(id=vault_id or uuid4(), name=name, note_count=0, is_active=False)


def _http_404_error() -> httpx.HTTPStatusError:
    request = httpx.Request('GET', 'http://memex/vaults/unknown')
    response = httpx.Response(status_code=404, request=request)
    return httpx.HTTPStatusError('404', request=request, response=response)


# -- Helper-level tests (AC-001..AC-006) --


def test_resolve_vault_ids_falls_back_to_bound_vault():
    """AC-001: missing ``vault_ids`` key returns ``[bound_vault_id]``."""
    api = Mock()
    api.resolve_vault_identifier = AsyncMock()
    api.list_vaults = AsyncMock()
    bound = uuid4()
    result = _resolve_vault_ids(api, {}, bound)
    assert result == [bound]
    api.resolve_vault_identifier.assert_not_awaited()
    api.list_vaults.assert_not_awaited()


def test_resolve_vault_ids_empty_list_falls_back_to_bound_vault():
    """AC-002: ``vault_ids=[]`` must NOT clear scope — falls back to bound vault."""
    api = Mock()
    api.resolve_vault_identifier = AsyncMock()
    bound = uuid4()
    result = _resolve_vault_ids(api, {'vault_ids': []}, bound)
    assert result == [bound]
    api.resolve_vault_identifier.assert_not_awaited()


def test_resolve_vault_ids_parses_uuid_strings_locally():
    """AC-003: raw UUID strings are parsed locally, never call resolve_vault_identifier."""
    api = Mock()
    api.resolve_vault_identifier = AsyncMock()
    uid_1 = uuid4()
    uid_2 = uuid4()
    result = _resolve_vault_ids(api, {'vault_ids': [str(uid_1), str(uid_2)]}, None)
    assert result == [uid_1, uid_2]
    api.resolve_vault_identifier.assert_not_awaited()


def test_resolve_vault_ids_calls_resolve_for_names():
    """AC-004: non-UUID names are resolved via ``api.resolve_vault_identifier``."""
    api = Mock()
    resolved_uuid = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved_uuid)
    result = _resolve_vault_ids(api, {'vault_ids': ['rituals']}, None)
    assert result == [resolved_uuid]
    api.resolve_vault_identifier.assert_awaited_once_with('rituals')


def test_resolve_vault_ids_wildcard_expands_to_all():
    """AC-005: ``["*"]`` expands to every vault via ``api.list_vaults()``."""
    api = Mock()
    v1 = _fake_vault()
    v2 = _fake_vault()
    api.list_vaults = AsyncMock(return_value=[v1, v2])
    api.resolve_vault_identifier = AsyncMock()
    result = _resolve_vault_ids(api, {'vault_ids': ['*']}, None)
    assert result == [v1.id, v2.id]
    api.list_vaults.assert_awaited_once()
    api.resolve_vault_identifier.assert_not_awaited()


def test_resolve_vault_ids_unknown_name_raises_tool_error(config, vault_id):
    """AC-006: unknown name → tool_error JSON (hard error, no silent fallback).

    ``api.resolve_vault_identifier`` raises ``httpx.HTTPStatusError`` on 404;
    the dispatcher converts that into tool_error with the failing name.
    """
    api = Mock()
    api.resolve_vault_identifier = AsyncMock(side_effect=_http_404_error())
    api.search = AsyncMock()
    out = dispatch(
        'memex_recall',
        {'query': 'x', 'vault_ids': ['nonexistent']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'nonexistent' in data['error']
    api.search.assert_not_awaited()


# -- Schema-level tests (AC-007, AC-008) --


def test_recall_schema_declares_vault_ids():
    """AC-007: ``RECALL_SCHEMA.vault_ids`` declared as array of string with wildcard note."""
    props = RECALL_SCHEMA['parameters']['properties']
    assert 'vault_ids' in props
    spec = props['vault_ids']
    assert spec['type'] == 'array'
    assert spec['items']['type'] == 'string'
    assert 'session-bound' in spec['description']
    assert '"*"' in spec['description']


def test_recall_schema_tags_description_disambiguates_vaults():
    """AC-008: ``tags`` description explicitly says it is NOT for vault selection."""
    tags_desc = RECALL_SCHEMA['parameters']['properties']['tags']['description']
    assert 'NOT for vault selection' in tags_desc
    assert 'vault_ids' in tags_desc


# -- Recall handler regressions (AC-009..AC-012) --


def test_recall_uses_bound_vault_when_vault_ids_omitted(config, vault_id):
    """AC-009: no ``vault_ids`` arg → ``api.search(vault_ids=[bound_vault_id], ...)``."""
    api = Mock()
    api.search = AsyncMock(return_value=[])
    dispatch('memex_recall', {'query': 'x'}, api=api, config=config, vault_id=vault_id)
    assert api.search.call_args.kwargs['vault_ids'] == [vault_id]


def test_recall_resolves_vault_names_via_helper(config, vault_id):
    """AC-010: ``vault_ids=["rituals"]`` → resolved UUID forwarded, name never leaks into ``tags``."""
    api = Mock()
    resolved = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved)
    api.search = AsyncMock(return_value=[])
    dispatch(
        'memex_recall',
        {'query': 'x', 'vault_ids': ['rituals']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.search.call_args.kwargs
    assert kwargs['vault_ids'] == [resolved]
    assert kwargs.get('tags') is None
    api.resolve_vault_identifier.assert_awaited_once_with('rituals')


def test_recall_wildcard_vault_ids_lists_all(config, vault_id):
    """AC-011: ``vault_ids=["*"]`` → ``api.list_vaults()`` result forwarded."""
    api = Mock()
    v1 = _fake_vault()
    v2 = _fake_vault()
    api.list_vaults = AsyncMock(return_value=[v1, v2])
    api.search = AsyncMock(return_value=[])
    dispatch(
        'memex_recall',
        {'query': 'x', 'vault_ids': ['*']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert api.search.call_args.kwargs['vault_ids'] == [v1.id, v2.id]


def test_recall_tags_are_forwarded_unchanged(config, vault_id):
    """AC-012: ``tags`` and ``vault_ids`` are independent knobs."""
    api = Mock()
    api.search = AsyncMock(return_value=[])
    dispatch(
        'memex_recall',
        {'query': 'x', 'tags': ['meeting']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.search.call_args.kwargs
    assert kwargs['tags'] == ['meeting']
    assert kwargs['vault_ids'] == [vault_id]


# -- Sibling handler regressions (AC-013..AC-017) --


def test_retrieve_notes_schema_declares_vault_ids():
    assert 'vault_ids' in RETRIEVE_NOTES_SCHEMA['parameters']['properties']


def test_retrieve_notes_uses_bound_vault_by_default(config, vault_id):
    """AC-013: ``handle_retrieve_notes`` mirrors recall — default is bound vault."""
    api = Mock()
    api.search_notes = AsyncMock(return_value=[])
    dispatch(
        'memex_retrieve_notes',
        {'query': 'doc'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert api.search_notes.call_args.kwargs['vault_ids'] == [vault_id]


def test_retrieve_notes_resolves_vault_names(config, vault_id):
    """AC-013: ``handle_retrieve_notes`` resolves names via the helper."""
    api = Mock()
    resolved = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved)
    api.search_notes = AsyncMock(return_value=[])
    dispatch(
        'memex_retrieve_notes',
        {'query': 'doc', 'vault_ids': ['rituals']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert api.search_notes.call_args.kwargs['vault_ids'] == [resolved]


def test_survey_schema_declares_vault_ids():
    assert 'vault_ids' in SURVEY_SCHEMA['parameters']['properties']


def test_survey_uses_bound_vault_by_default(config, vault_id):
    """AC-014: ``handle_survey`` defaults to bound vault."""
    api = Mock()
    response = SurveyResponse(
        query='q', sub_queries=[], topics=[], total_notes=0, total_facts=0, truncated=False
    )
    api.survey = AsyncMock(return_value=response)
    dispatch('memex_survey', {'query': 'q'}, api=api, config=config, vault_id=vault_id)
    assert api.survey.call_args.kwargs['vault_ids'] == [vault_id]


def test_survey_resolves_vault_names(config, vault_id):
    """AC-014: ``handle_survey`` resolves names via the helper."""
    api = Mock()
    resolved = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved)
    response = SurveyResponse(
        query='q', sub_queries=[], topics=[], total_notes=0, total_facts=0, truncated=False
    )
    api.survey = AsyncMock(return_value=response)
    dispatch(
        'memex_survey',
        {'query': 'q', 'vault_ids': ['rituals']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert api.survey.call_args.kwargs['vault_ids'] == [resolved]


def test_list_entities_schema_declares_vault_ids():
    assert 'vault_ids' in LIST_ENTITIES_SCHEMA['parameters']['properties']


def test_list_entities_uses_bound_vault_by_default(config, vault_id):
    """AC-015: ``handle_list_entities`` forwards ``vault_ids=[bound]`` (not scalar ``vault_id=``)."""
    api = Mock()
    api.search_entities = AsyncMock(return_value=[])
    dispatch(
        'memex_list_entities',
        {'query': 'Rust'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.search_entities.call_args.kwargs
    assert kwargs['vault_ids'] == [vault_id]
    assert 'vault_id' not in kwargs


def test_list_entities_forwards_vault_ids_list(config, vault_id):
    """AC-015: ``vault_ids=["rituals"]`` → resolved UUID forwarded as a list."""
    api = Mock()
    resolved = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved)
    api.search_entities = AsyncMock(return_value=[])
    dispatch(
        'memex_list_entities',
        {'query': 'Rust', 'vault_ids': ['rituals']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.search_entities.call_args.kwargs
    assert kwargs['vault_ids'] == [resolved]
    assert 'vault_id' not in kwargs


def test_entity_mentions_schema_declares_vault_ids():
    assert 'vault_ids' in GET_ENTITY_MENTIONS_SCHEMA['parameters']['properties']


def test_entity_mentions_uses_bound_vault_by_default(config, vault_id):
    """AC-016: ``handle_get_entity_mentions`` forwards ``vault_ids=[bound]``."""
    api = Mock()
    api.get_entity_mentions = AsyncMock(return_value=[])
    dispatch(
        'memex_get_entity_mentions',
        {'entity_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.get_entity_mentions.call_args.kwargs
    assert kwargs['vault_ids'] == [vault_id]
    assert 'vault_id' not in kwargs


def test_entity_mentions_forwards_vault_ids_list(config, vault_id):
    """AC-016: ``vault_ids=["rituals"]`` → resolved UUID forwarded as a list."""
    api = Mock()
    resolved = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved)
    api.get_entity_mentions = AsyncMock(return_value=[])
    dispatch(
        'memex_get_entity_mentions',
        {'entity_id': str(uuid4()), 'vault_ids': ['rituals']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.get_entity_mentions.call_args.kwargs
    assert kwargs['vault_ids'] == [resolved]
    assert 'vault_id' not in kwargs


def test_entity_cooccurrences_schema_declares_vault_ids():
    assert 'vault_ids' in GET_ENTITY_COOCCURRENCES_SCHEMA['parameters']['properties']


def test_entity_cooccurrences_uses_bound_vault_by_default(config, vault_id):
    """AC-017: ``handle_get_entity_cooccurrences`` forwards ``vault_ids=[bound]``."""
    api = Mock()
    api.get_entity_cooccurrences = AsyncMock(return_value=[])
    dispatch(
        'memex_get_entity_cooccurrences',
        {'entity_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.get_entity_cooccurrences.call_args.kwargs
    assert kwargs['vault_ids'] == [vault_id]
    assert 'vault_id' not in kwargs


def test_entity_cooccurrences_forwards_vault_ids_list(config, vault_id):
    """AC-017: ``vault_ids=["rituals"]`` → resolved UUID forwarded as a list."""
    api = Mock()
    resolved = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved)
    api.get_entity_cooccurrences = AsyncMock(return_value=[])
    dispatch(
        'memex_get_entity_cooccurrences',
        {'entity_id': str(uuid4()), 'vault_ids': ['rituals']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.get_entity_cooccurrences.call_args.kwargs
    assert kwargs['vault_ids'] == [resolved]
    assert 'vault_id' not in kwargs


def test_vault_resolution_error_carries_failing_name():
    """``VaultResolutionError`` preserves the failing name on ``.name``."""
    err = VaultResolutionError('missing-vault')
    assert err.name == 'missing-vault'
    assert str(err) == 'missing-vault'


# ---------------------------------------------------------------------------
# Stream 4: lifecycle + template tools
#
# Covers AC-027..AC-035, AC-051..AC-053, AC-073..AC-077 — the six Stream 4
# tools that expose note lifecycle operations (rename, status, user_notes)
# and the client-side ``TemplateRegistry`` (get, list, register).
# ---------------------------------------------------------------------------

from unittest.mock import patch  # noqa: E402

from memex_hermes_plugin.memex.tools import (  # noqa: E402
    GET_TEMPLATE_SCHEMA,
    LIST_TEMPLATES_SCHEMA,
    REGISTER_TEMPLATE_SCHEMA,
    RENAME_NOTE_SCHEMA,
    SET_NOTE_STATUS_SCHEMA,
    UPDATE_USER_NOTES_SCHEMA,
)


# -- Schema registration (AC-027, AC-051, AC-073) ----------------------------


def test_stream_4_schemas_registered_in_all_schemas():
    """AC-027/AC-051/AC-073: all six Stream 4 schemas are in ALL_SCHEMAS."""
    names = {s['name'] for s in ALL_SCHEMAS}
    assert 'memex_set_note_status' in names
    assert 'memex_update_user_notes' in names
    assert 'memex_rename_note' in names
    assert 'memex_get_template' in names
    assert 'memex_list_templates' in names
    assert 'memex_register_template' in names


def test_set_note_status_schema_requires_note_id_and_status():
    props = SET_NOTE_STATUS_SCHEMA['parameters']['properties']
    required = SET_NOTE_STATUS_SCHEMA['parameters']['required']
    assert 'note_id' in props and 'status' in props and 'linked_note_id' in props
    assert set(required) == {'note_id', 'status'}


def test_update_user_notes_schema_allows_null_user_notes():
    """AC-052: ``user_notes`` may be null (for deletion)."""
    props = UPDATE_USER_NOTES_SCHEMA['parameters']['properties']
    assert 'null' in props['user_notes']['type']


def test_rename_note_schema_requires_both_fields():
    required = RENAME_NOTE_SCHEMA['parameters']['required']
    assert set(required) == {'note_id', 'new_title'}


def test_get_template_schema_requires_slug():
    assert GET_TEMPLATE_SCHEMA['parameters']['required'] == ['slug']


def test_list_templates_schema_has_no_required_params():
    assert LIST_TEMPLATES_SCHEMA['parameters'].get('required', []) == []


def test_register_template_schema_requires_slug_and_template():
    required = REGISTER_TEMPLATE_SCHEMA['parameters']['required']
    assert set(required) == {'slug', 'template'}


# -- set_note_status (AC-027..AC-030) ----------------------------------------


@pytest.mark.parametrize('status', ['active', 'superseded', 'appended', 'archived'])
def test_set_note_status_accepts_all_four_statuses(config, vault_id, status):
    """AC-028: all four documented statuses are accepted client-side."""
    api = Mock()
    api.set_note_status = AsyncMock(return_value={'status': status})
    note_uuid = uuid4()
    out = dispatch(
        'memex_set_note_status',
        {'note_id': str(note_uuid), 'status': status},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['status'] == status
    api.set_note_status.assert_awaited_once()
    call_args = api.set_note_status.call_args
    assert call_args.args[0] == note_uuid
    assert call_args.args[1] == status
    assert call_args.args[2] is None  # linked_note_id


def test_set_note_status_rejects_unknown_status(config, vault_id):
    """AC-029: unknown status is rejected client-side (never calls the API)."""
    api = Mock()
    api.set_note_status = AsyncMock()
    out = dispatch(
        'memex_set_note_status',
        {'note_id': str(uuid4()), 'status': 'bogus'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'bogus' in data['error']
    api.set_note_status.assert_not_awaited()


def test_set_note_status_forwards_linked_note_id(config, vault_id):
    """AC-030: linked_note_id UUID is parsed and forwarded."""
    api = Mock()
    api.set_note_status = AsyncMock(return_value={})
    note_uuid = uuid4()
    linked_uuid = uuid4()
    dispatch(
        'memex_set_note_status',
        {
            'note_id': str(note_uuid),
            'status': 'superseded',
            'linked_note_id': str(linked_uuid),
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_args = api.set_note_status.call_args
    assert call_args.args[2] == linked_uuid


def test_set_note_status_rejects_invalid_note_uuid(config, vault_id):
    api = Mock()
    api.set_note_status = AsyncMock()
    out = dispatch(
        'memex_set_note_status',
        {'note_id': 'not-a-uuid', 'status': 'active'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    api.set_note_status.assert_not_awaited()


def test_set_note_status_requires_note_id_and_status(config, vault_id):
    api = Mock()
    api.set_note_status = AsyncMock()
    out = dispatch('memex_set_note_status', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)
    api.set_note_status.assert_not_awaited()


def test_set_note_status_forwards_api_errors(config, vault_id):
    api = Mock()
    api.set_note_status = AsyncMock(side_effect=RuntimeError('boom'))
    out = dispatch(
        'memex_set_note_status',
        {'note_id': str(uuid4()), 'status': 'active'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'boom' in json.loads(out)['error']


# -- update_user_notes (AC-052..AC-053) --------------------------------------


def test_update_user_notes_forwards_text(config, vault_id):
    api = Mock()
    api.update_user_notes = AsyncMock(return_value={'note_id': 'x', 'updated': True})
    note_uuid = uuid4()
    dispatch(
        'memex_update_user_notes',
        {'note_id': str(note_uuid), 'user_notes': 'new annotations'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_args = api.update_user_notes.call_args
    assert call_args.args[0] == note_uuid
    assert call_args.args[1] == 'new annotations'


def test_update_user_notes_explicit_null_clears_annotations(config, vault_id):
    """AC-052: passing null (Python ``None``) clears annotations."""
    api = Mock()
    api.update_user_notes = AsyncMock(return_value={'cleared': True})
    note_uuid = uuid4()
    dispatch(
        'memex_update_user_notes',
        {'note_id': str(note_uuid), 'user_notes': None},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_args = api.update_user_notes.call_args
    assert call_args.args[1] is None


def test_update_user_notes_missing_key_clears_annotations(config, vault_id):
    """AC-052 (addendum): omitting ``user_notes`` is equivalent to null."""
    api = Mock()
    api.update_user_notes = AsyncMock(return_value={'cleared': True})
    note_uuid = uuid4()
    dispatch(
        'memex_update_user_notes',
        {'note_id': str(note_uuid)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_args = api.update_user_notes.call_args
    assert call_args.args[1] is None


def test_update_user_notes_invalid_uuid(config, vault_id):
    api = Mock()
    api.update_user_notes = AsyncMock()
    out = dispatch(
        'memex_update_user_notes',
        {'note_id': 'nope'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.update_user_notes.assert_not_awaited()


def test_update_user_notes_requires_note_id(config, vault_id):
    api = Mock()
    api.update_user_notes = AsyncMock()
    out = dispatch('memex_update_user_notes', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)
    api.update_user_notes.assert_not_awaited()


# -- rename_note (AC-053) ----------------------------------------------------


def test_rename_note_forwards_to_update_note_title(config, vault_id):
    api = Mock()
    api.update_note_title = AsyncMock(return_value={'ok': True})
    note_uuid = uuid4()
    out = dispatch(
        'memex_rename_note',
        {'note_id': str(note_uuid), 'new_title': 'A New Name'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['status'] == 'ok'
    assert data['note_id'] == str(note_uuid)
    assert data['new_title'] == 'A New Name'
    api.update_note_title.assert_awaited_once_with(note_uuid, 'A New Name')


def test_rename_note_invalid_uuid(config, vault_id):
    api = Mock()
    api.update_note_title = AsyncMock()
    out = dispatch(
        'memex_rename_note',
        {'note_id': 'nope', 'new_title': 'x'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.update_note_title.assert_not_awaited()


def test_rename_note_requires_both_args(config, vault_id):
    api = Mock()
    api.update_note_title = AsyncMock()
    out = dispatch(
        'memex_rename_note',
        {'note_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.update_note_title.assert_not_awaited()


# -- Templates (AC-031..AC-035, AC-074..AC-077) ------------------------------


def _fake_template_info(slug: str = 'default', source: str = 'builtin'):
    from memex_common.templates import TemplateInfo

    return TemplateInfo(
        slug=slug,
        display_name=slug.replace('_', ' ').title(),
        description=f'A {slug} template',
        source=source,
    )


def test_get_template_uses_template_registry(config, vault_id):
    """AC-031/AC-032: uses ``TemplateRegistry.get_template`` synchronously.

    ``get_template`` is a *sync* method — it must be called, never awaited.
    """
    registry = Mock()
    registry.get_template = Mock(return_value='# A template\nBody')

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_get_template',
            {'slug': 'default'},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert data['slug'] == 'default'
    assert data['content'] == '# A template\nBody'
    registry.get_template.assert_called_once_with('default')


def test_get_template_unknown_slug_returns_error(config, vault_id):
    """AC-033/AC-034: ``KeyError`` from registry → tool_error (not ValueError)."""
    registry = Mock()
    registry.get_template = Mock(side_effect=KeyError('Unknown template: wat'))

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_get_template',
            {'slug': 'wat'},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert 'error' in data
    assert 'wat' in data['error']


def test_get_template_only_wraps_key_error(config, vault_id):
    """AC-034: ``ValueError`` from registry is NOT masked as KeyError.

    ``ValueError`` comes from misuse (e.g. invalid scope). We still return
    tool_error, but the error message carries the original exception — we do
    not convert it into a KeyError-shaped "Unknown template" message.
    """
    registry = Mock()
    registry.get_template = Mock(side_effect=ValueError('malformed'))

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_get_template',
            {'slug': 'x'},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert 'error' in data
    assert 'Unknown template' not in data['error']
    assert 'malformed' in data['error']


def test_get_template_requires_slug(config, vault_id):
    out = dispatch('memex_get_template', {}, api=Mock(), config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)


def test_list_templates_uses_template_registry_synchronously(config, vault_id):
    """AC-074: ``TemplateRegistry.list_templates`` is sync — called, never awaited."""
    registry = Mock()
    registry.list_templates = Mock(
        return_value=[
            _fake_template_info('default', 'builtin'),
            _fake_template_info('project', 'local'),
        ]
    )

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_list_templates',
            {},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert data['count'] == 2
    slugs = {r['slug'] for r in data['results']}
    assert slugs == {'default', 'project'}
    sources = {r['source'] for r in data['results']}
    assert sources == {'builtin', 'local'}
    registry.list_templates.assert_called_once_with()


def test_list_templates_returns_empty_on_no_templates(config, vault_id):
    """AC-075: empty registry returns an empty result set, not an error."""
    registry = Mock()
    registry.list_templates = Mock(return_value=[])
    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_list_templates',
            {},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )
    data = json.loads(out)
    assert data['count'] == 0
    assert data['results'] == []


def test_register_template_calls_register_from_content(config, vault_id):
    """AC-076/AC-077: calls ``register_from_content`` sync with scope=global."""
    registry = Mock()
    info = _fake_template_info('sprint_retro', 'global')
    registry.register_from_content = Mock(return_value=info)

    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_register_template',
            {
                'slug': 'sprint_retro',
                'template': '---\ntitle: ok\n---\nbody',
                'name': 'Sprint Retro',
                'description': 'A retrospective.',
            },
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )

    data = json.loads(out)
    assert data['slug'] == 'sprint_retro'
    assert data['source'] == 'global'
    call_kwargs = registry.register_from_content.call_args.kwargs
    assert call_kwargs['slug'] == 'sprint_retro'
    assert call_kwargs['template'].startswith('---')
    assert call_kwargs['name'] == 'Sprint Retro'
    assert call_kwargs['description'] == 'A retrospective.'
    assert call_kwargs['scope'] == 'global'


def test_register_template_requires_slug_and_template(config, vault_id):
    out = dispatch(
        'memex_register_template',
        {'slug': 'x'},
        api=Mock(),
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)


def test_register_template_wraps_registry_errors(config, vault_id):
    """Registry errors (invalid TOML, filesystem, …) surface as tool_error."""
    registry = Mock()
    registry.register_from_content = Mock(side_effect=OSError('disk full'))
    with patch(
        'memex_hermes_plugin.memex.tools._build_template_registry',
        return_value=registry,
    ):
        out = dispatch(
            'memex_register_template',
            {'slug': 'x', 'template': 'body'},
            api=Mock(),
            config=config,
            vault_id=vault_id,
        )
    assert 'disk full' in json.loads(out)['error']


def test_template_registry_built_with_builtin_global_local_layers(tmp_path):
    """AC-077: ``_build_template_registry`` assembles builtin → global → local
    in that order, mirroring the MCP pattern.
    """
    from memex_hermes_plugin.memex.tools import _build_template_registry

    with patch('memex_common.config.MemexConfig') as MockConfig:
        mock_cfg = Mock()
        mock_cfg.server.file_store.root = str(tmp_path)
        MockConfig.return_value = mock_cfg

        registry = _build_template_registry()

    assert hasattr(registry, 'get_template')
    assert hasattr(registry, 'list_templates')
    assert hasattr(registry, 'register_from_content')
    labels = [label for label, _ in registry._template_dirs]  # noqa: SLF001
    assert labels[0] == 'builtin'
    assert 'global' in labels
    assert labels[-1] == 'local'
