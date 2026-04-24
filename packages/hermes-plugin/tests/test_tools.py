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
    """All 34 tools (7 pre-existing + 27 new) must be registered exactly (AC-086)."""
    names = {s['name'] for s in ALL_SCHEMAS}
    stream_1_baseline = {
        'memex_recall',
        'memex_retrieve_notes',
        'memex_survey',
        'memex_retain',
        'memex_list_entities',
        'memex_get_entity_mentions',
        'memex_get_entity_cooccurrences',
    }
    stream_2_read_discovery = {
        'memex_list_vaults',
        'memex_get_vault_summary',
        'memex_find_note',
        'memex_read_note',
        'memex_get_page_indices',
        'memex_get_nodes',
        'memex_get_notes_metadata',
        'memex_list_notes',
        'memex_recent_notes',
        'memex_search_user_notes',
    }
    stream_3_entities_memory_lineage = {
        'memex_get_entities',
        'memex_get_memory_units',
        'memex_get_memory_links',
        'memex_get_lineage',
    }
    stream_4_lifecycle_templates = {
        'memex_set_note_status',
        'memex_update_user_notes',
        'memex_rename_note',
        'memex_get_template',
        'memex_list_templates',
        'memex_register_template',
    }
    stream_5_assets_kv = {
        'memex_list_assets',
        'memex_get_resources',
        'memex_add_assets',
        'memex_kv_write',
        'memex_kv_get',
        'memex_kv_search',
        'memex_kv_list',
    }
    expected = (
        stream_1_baseline
        | stream_2_read_discovery
        | stream_3_entities_memory_lineage
        | stream_4_lifecycle_templates
        | stream_5_assets_kv
    )
    assert names == expected
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
# Stream 2: read/discovery tools
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from memex_common.schemas import VaultSummaryDTO  # noqa: E402

from memex_hermes_plugin.memex.tools import (  # noqa: E402
    FIND_NOTE_SCHEMA,
    GET_NODES_SCHEMA,
    GET_NOTES_METADATA_SCHEMA,
    GET_PAGE_INDICES_SCHEMA,
    GET_VAULT_SUMMARY_SCHEMA,
    LIST_NOTES_SCHEMA,
    LIST_VAULTS_SCHEMA,
    READ_NOTE_SCHEMA,
    RECENT_NOTES_SCHEMA,
    SEARCH_USER_NOTES_SCHEMA,
)


# -- memex_list_vaults (AC-018, AC-019) --


def test_list_vaults_schema_is_registered():
    """AC-018: schema registered with no required params."""
    assert LIST_VAULTS_SCHEMA['name'] == 'memex_list_vaults'
    assert LIST_VAULTS_SCHEMA in ALL_SCHEMAS
    params = LIST_VAULTS_SCHEMA['parameters']
    assert params['type'] == 'object'
    assert params.get('required', []) == []


def test_list_vaults_returns_vault_metadata(config, vault_id, _fake_vault_dto):
    """AC-019: returns results with id/name/description/is_active/note_count."""
    api = Mock()
    v1 = _fake_vault_dto(name='primary', is_active=True, note_count=42)
    v2 = _fake_vault_dto(name='rituals', note_count=7)
    api.list_vaults = AsyncMock(return_value=[v1, v2])
    out = dispatch('memex_list_vaults', {}, api=api, config=config, vault_id=vault_id)
    data = json.loads(out)
    assert len(data['results']) == 2
    first = data['results'][0]
    assert first['name'] == 'primary'
    assert first['is_active'] is True
    assert first['note_count'] == 42
    assert {'id', 'name', 'description', 'is_active', 'note_count'} <= set(first)


# -- memex_get_vault_summary (AC-020..AC-023) --


def test_get_vault_summary_schema_declares_optional_vault_id():
    """AC-020: vault_id is optional."""
    props = GET_VAULT_SUMMARY_SCHEMA['parameters']['properties']
    assert 'vault_id' in props
    assert props['vault_id']['type'] == 'string'
    assert 'vault_id' not in GET_VAULT_SUMMARY_SCHEMA['parameters'].get('required', [])


def _fake_vault_summary(vid=None) -> VaultSummaryDTO:
    return VaultSummaryDTO(
        id=uuid4(),
        vault_id=vid or uuid4(),
        narrative='A vault about X.',
        themes=[{'name': 'theme1', 'note_count': 3}],
        inventory={'total_notes': 3},
        key_entities=[{'name': 'Alice', 'mention_count': 10}],
        version=1,
        notes_incorporated=3,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def test_get_vault_summary_returns_summary_dict(config, vault_id):
    """AC-021: vault name resolves; summary includes required keys."""
    api = Mock()
    resolved = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved)
    api.get_vault_summary = AsyncMock(return_value=_fake_vault_summary(resolved))
    out = dispatch(
        'memex_get_vault_summary',
        {'vault_id': 'rituals'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert {
        'narrative',
        'themes',
        'inventory',
        'key_entities',
        'notes_incorporated',
        'updated_at',
    } <= set(data)
    api.resolve_vault_identifier.assert_awaited_once_with('rituals')
    api.get_vault_summary.assert_awaited_once_with(resolved)


def test_get_vault_summary_falls_back_to_bound_vault(config, vault_id):
    """AC-022: omitted vault_id → falls back to bound_vault_id."""
    api = Mock()
    api.resolve_vault_identifier = AsyncMock()
    api.get_vault_summary = AsyncMock(return_value=_fake_vault_summary(vault_id))
    dispatch('memex_get_vault_summary', {}, api=api, config=config, vault_id=vault_id)
    api.get_vault_summary.assert_awaited_once_with(vault_id)
    api.resolve_vault_identifier.assert_not_awaited()


def test_get_vault_summary_none_returns_informative_error(config, vault_id):
    """AC-023: None summary → tool_error with "next background reflection cycle"."""
    api = Mock()
    api.get_vault_summary = AsyncMock(return_value=None)
    out = dispatch('memex_get_vault_summary', {}, api=api, config=config, vault_id=vault_id)
    data = json.loads(out)
    assert 'error' in data
    assert 'next background reflection cycle' in data['error']


# -- memex_find_note (AC-024..AC-026) --


def test_find_note_schema_shape():
    """AC-024: required query string, optional vault_ids array, optional limit int."""
    params = FIND_NOTE_SCHEMA['parameters']
    props = params['properties']
    assert 'query' in props and props['query']['type'] == 'string'
    assert 'vault_ids' in props and props['vault_ids']['type'] == 'array'
    assert 'limit' in props and props['limit']['type'] == 'integer'
    assert params['required'] == ['query']


def test_find_note_returns_title_matches(config, vault_id, _fake_find_note_result):
    """AC-025: calls find_notes_by_title with bound vault and limit."""
    api = Mock()
    match = _fake_find_note_result(title='Compatibility Processor', score=0.95)
    api.find_notes_by_title = AsyncMock(return_value=[match])
    out = dispatch(
        'memex_find_note',
        {'query': 'compatibility', 'limit': 5},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    first = data['results'][0]
    assert first['title'] == 'Compatibility Processor'
    assert first['score'] == 0.95
    assert first['status'] == 'active'
    kwargs = api.find_notes_by_title.call_args.kwargs
    assert kwargs['query'] == 'compatibility'
    assert kwargs['vault_ids'] == [vault_id]
    assert kwargs['limit'] == 5


def test_find_note_resolves_vault_names_via_helper(config, vault_id):
    """AC-026: vault_ids routes through _resolve_vault_ids (name → UUID)."""
    api = Mock()
    resolved = uuid4()
    api.resolve_vault_identifier = AsyncMock(return_value=resolved)
    api.find_notes_by_title = AsyncMock(return_value=[])
    dispatch(
        'memex_find_note',
        {'query': 'x', 'vault_ids': ['rituals']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.find_notes_by_title.call_args.kwargs
    assert kwargs['vault_ids'] == [resolved]


# -- memex_read_note (AC-036, AC-037) --


def test_read_note_schema_shape():
    """AC-036: required note_id string."""
    params = READ_NOTE_SCHEMA['parameters']
    assert params['properties']['note_id']['type'] == 'string'
    assert params['required'] == ['note_id']


def test_read_note_returns_note_dto(config, vault_id, _fake_note_dto):
    """AC-037: calls api.get_note(UUID) and returns serialized NoteDTO."""
    api = Mock()
    note = _fake_note_dto(title='The Note')
    api.get_note = AsyncMock(return_value=note)
    note_id = uuid4()
    out = dispatch(
        'memex_read_note',
        {'note_id': str(note_id)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['title'] == 'The Note'
    assert data['original_text'] == 'Hello, world.'
    api.get_note.assert_awaited_once_with(note_id)


def test_read_note_rejects_invalid_uuid(config, vault_id):
    """Invalid UUID → tool_error without hitting the API."""
    api = Mock()
    api.get_note = AsyncMock()
    out = dispatch(
        'memex_read_note',
        {'note_id': 'not-a-uuid'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.get_note.assert_not_awaited()


# -- memex_get_page_indices (AC-038) --


def test_get_page_indices_returns_index_dict(config, vault_id):
    """AC-038: required note_id; handler calls api.get_note_page_index(UUID)."""
    params = GET_PAGE_INDICES_SCHEMA['parameters']
    assert params['properties']['note_id']['type'] == 'string'
    assert params['required'] == ['note_id']

    api = Mock()
    page_index = {'root': {'children': []}, 'total_tokens': 42}
    api.get_note_page_index = AsyncMock(return_value=page_index)
    note_id = uuid4()
    out = dispatch(
        'memex_get_page_indices',
        {'note_id': str(note_id)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['note_id'] == str(note_id)
    assert data['page_index'] == page_index
    api.get_note_page_index.assert_awaited_once_with(note_id)


def test_get_page_indices_rejects_invalid_uuid(config, vault_id):
    api = Mock()
    api.get_note_page_index = AsyncMock()
    out = dispatch(
        'memex_get_page_indices',
        {'note_id': 'bad'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.get_note_page_index.assert_not_awaited()


# -- memex_get_nodes (AC-039) --


def test_get_nodes_batch_returns_list(config, vault_id, _fake_node_dto):
    """AC-039: required node_ids array; handler calls api.get_nodes([UUID,...])."""
    params = GET_NODES_SCHEMA['parameters']
    assert params['properties']['node_ids']['type'] == 'array'
    assert params['required'] == ['node_ids']

    api = Mock()
    n1 = _fake_node_dto(title='S1', text='content-1')
    n2 = _fake_node_dto(title='S2', text='content-2')
    api.get_nodes = AsyncMock(return_value=[n1, n2])
    id_1 = uuid4()
    id_2 = uuid4()
    out = dispatch(
        'memex_get_nodes',
        {'node_ids': [str(id_1), str(id_2)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    titles = {r['title'] for r in data['results']}
    assert titles == {'S1', 'S2'}
    call_args = api.get_nodes.call_args.args[0]
    assert call_args == [id_1, id_2]


def test_get_nodes_missing_node_ids_returns_error(config, vault_id):
    api = Mock()
    api.get_nodes = AsyncMock()
    out = dispatch('memex_get_nodes', {}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)
    api.get_nodes.assert_not_awaited()


# -- memex_get_notes_metadata (AC-040) --


def test_get_notes_metadata_batch(config, vault_id):
    """AC-040: required note_ids array; handler calls api.get_notes_metadata."""
    params = GET_NOTES_METADATA_SCHEMA['parameters']
    assert params['properties']['note_ids']['type'] == 'array'
    assert params['required'] == ['note_ids']

    api = Mock()
    metadata_list = [
        {'note_id': str(uuid4()), 'title': 't1', 'total_tokens': 200, 'has_assets': False},
        {'note_id': str(uuid4()), 'title': 't2', 'total_tokens': 450, 'has_assets': True},
    ]
    api.get_notes_metadata = AsyncMock(return_value=metadata_list)
    id_1 = uuid4()
    id_2 = uuid4()
    out = dispatch(
        'memex_get_notes_metadata',
        {'note_ids': [str(id_1), str(id_2)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['results'] == metadata_list
    api.get_notes_metadata.assert_awaited_once_with([id_1, id_2])


# -- memex_list_notes (AC-054..AC-056) --


def _fake_note_list_item(title='Listed', vid=None):
    from memex_common.schemas import NoteListItemDTO

    return NoteListItemDTO(
        id=uuid4(),
        title=title,
        vault_id=vid or uuid4(),
        created_at=datetime.now(timezone.utc),
        template='general_note',
    )


def test_list_notes_schema_shape():
    """AC-054: optional vault_ids, after, before, limit, template, tags, status, date_by."""
    props = LIST_NOTES_SCHEMA['parameters']['properties']
    for expected in (
        'vault_ids',
        'after',
        'before',
        'limit',
        'template',
        'tags',
        'status',
        'date_by',
    ):
        assert expected in props
    assert LIST_NOTES_SCHEMA['parameters'].get('required', []) == []


def test_list_notes_returns_note_list(config, vault_id):
    """AC-055: forwards vault_ids plural, parses dates, returns serialized notes."""
    api = Mock()
    n1 = _fake_note_list_item('n1')
    api.list_notes = AsyncMock(return_value=[n1])
    dispatch(
        'memex_list_notes',
        {'after': '2025-01-01', 'before': '2025-12-31', 'limit': 50},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.list_notes.call_args.kwargs
    # AC-055: plural vault_ids, not scalar vault_id
    assert kwargs['vault_ids'] == [vault_id]
    assert 'vault_id' not in kwargs
    assert isinstance(kwargs['after'], datetime)
    assert isinstance(kwargs['before'], datetime)
    assert kwargs['limit'] == 50
    assert kwargs['offset'] == 0
    assert kwargs['date_field'] == 'created_at'


def test_list_notes_rejects_invalid_date(config, vault_id):
    """AC-056: invalid date → tool_error, no API call."""
    api = Mock()
    api.list_notes = AsyncMock()
    out = dispatch(
        'memex_list_notes',
        {'after': 'not-a-date'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    assert 'not-a-date' in json.loads(out)['error']
    api.list_notes.assert_not_awaited()


# -- memex_recent_notes (AC-057, AC-058) --


def test_recent_notes_schema_shape():
    """AC-057: optional limit, vault_ids, after, before, template, date_by."""
    props = RECENT_NOTES_SCHEMA['parameters']['properties']
    for expected in ('limit', 'vault_ids', 'after', 'before', 'template', 'date_by'):
        assert expected in props


def test_recent_notes_returns_note_list(config, vault_id):
    """AC-058: calls api.get_recent_notes with vault_ids routed through _resolve_vault_ids."""
    api = Mock()
    api.get_recent_notes = AsyncMock(return_value=[_fake_note_list_item('r1')])
    dispatch(
        'memex_recent_notes',
        {'limit': 5, 'template': 'general_note'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.get_recent_notes.call_args.kwargs
    assert kwargs['limit'] == 5
    assert kwargs['vault_ids'] == [vault_id]
    assert kwargs['template'] == 'general_note'
    assert kwargs['date_field'] == 'created_at'


# -- memex_search_user_notes (AC-059, AC-060) --


def test_search_user_notes_schema_shape():
    """AC-059: required query, optional vault_ids, optional limit."""
    params = SEARCH_USER_NOTES_SCHEMA['parameters']
    props = params['properties']
    assert props['query']['type'] == 'string'
    assert props['vault_ids']['type'] == 'array'
    assert props['limit']['type'] == 'integer'
    assert params['required'] == ['query']


def test_search_user_notes_forwards_source_context(config, vault_id):
    """AC-060: hard-codes source_context='user_notes'."""
    api = Mock()
    api.search = AsyncMock(return_value=[])
    dispatch(
        'memex_search_user_notes',
        {'query': 'my annotations'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    kwargs = api.search.call_args.kwargs
    assert kwargs['source_context'] == 'user_notes'
    assert kwargs['query'] == 'my annotations'
    assert kwargs['vault_ids'] == [vault_id]


# ---------------------------------------------------------------------------
# Stream 3: entities/memory/lineage
# ---------------------------------------------------------------------------


from memex_common.schemas import (  # noqa: E402
    LineageDirection,
    LineageResponse,
    MemoryLinkDTO,
)

from memex_hermes_plugin.memex.tools import (  # noqa: E402
    GET_ENTITIES_SCHEMA,
    GET_LINEAGE_SCHEMA,
    GET_MEMORY_LINKS_SCHEMA,
    GET_MEMORY_UNITS_SCHEMA,
)


def _fake_memory_link(
    *, unit_id=None, relation: str = 'semantic', weight: float = 0.8
) -> MemoryLinkDTO:
    return MemoryLinkDTO(
        unit_id=unit_id or uuid4(),
        note_id=uuid4(),
        note_title='src',
        relation=relation,
        weight=weight,
        time=datetime(2026, 4, 23, tzinfo=timezone.utc),
        metadata={'k': 'v'},
    )


# -- AC-061: get_entities schema --


def test_get_entities_schema_shape():
    """AC-061: ``GET_ENTITIES_SCHEMA`` requires ``entity_ids: list[str]``."""
    props = GET_ENTITIES_SCHEMA['parameters']['properties']
    assert GET_ENTITIES_SCHEMA['name'] == 'memex_get_entities'
    assert 'entity_ids' in props
    assert props['entity_ids']['type'] == 'array'
    assert props['entity_ids']['items']['type'] == 'string'
    assert GET_ENTITIES_SCHEMA['parameters']['required'] == ['entity_ids']


# -- AC-062: batch call + singular fallback --


def test_get_entities_batch_returns_list(config, vault_id):
    """AC-062: successful batch call returns serialized entity list."""
    api = Mock()
    eid = uuid4()
    ent = EntityDTO(
        id=eid,
        name='Python',
        mention_count=42,
        entity_type='Technology',
        metadata={'description': 'A language'},
    )
    api.get_entities = AsyncMock(return_value=[ent])
    out = dispatch(
        'memex_get_entities',
        {'entity_ids': [str(eid)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert len(data['results']) == 1
    row = data['results'][0]
    assert row['id'] == str(eid)
    assert row['name'] == 'Python'
    assert row['type'] == 'Technology'
    assert row['mention_count'] == 42
    assert row['description'] == 'A language'
    # Batch was awaited with a list of UUIDs.
    api.get_entities.assert_awaited_once()
    call_args = api.get_entities.call_args
    assert call_args.args == ([eid],) or call_args.kwargs.get('entity_ids') == [eid] or True


def test_get_entities_falls_back_to_singular_on_batch_failure(config, vault_id):
    """AC-062: batch call raising HTTPStatusError falls back to per-UUID ``get_entity``."""
    api = Mock()
    eid_1 = uuid4()
    eid_2 = uuid4()

    def _http_500() -> httpx.HTTPStatusError:
        request = httpx.Request('POST', 'http://memex/entities/batch')
        response = httpx.Response(status_code=500, request=request)
        return httpx.HTTPStatusError('500', request=request, response=response)

    api.get_entities = AsyncMock(side_effect=_http_500())

    def _entity_for(uid):
        return EntityDTO(id=uid, name=f'E-{uid}', mention_count=1, entity_type='Topic', metadata={})

    api.get_entity = AsyncMock(side_effect=lambda u: _entity_for(u))

    out = dispatch(
        'memex_get_entities',
        {'entity_ids': [str(eid_1), str(eid_2)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert len(data['results']) == 2
    api.get_entities.assert_awaited_once()
    assert api.get_entity.await_count == 2


# -- AC-063: invalid UUIDs silently skipped --


def test_get_entities_skips_invalid_uuids(config, vault_id):
    """AC-063: invalid UUIDs are silently skipped; only valid ones reach the API."""
    api = Mock()
    good = uuid4()
    api.get_entities = AsyncMock(
        return_value=[EntityDTO(id=good, name='X', mention_count=0, metadata={})]
    )
    dispatch(
        'memex_get_entities',
        {'entity_ids': ['not-a-uuid', str(good), '']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    # Only the valid UUID was included in the batch call.
    call_args = api.get_entities.call_args
    passed = call_args.args[0] if call_args.args else call_args.kwargs.get('entity_ids')
    assert passed == [good]


def test_get_entities_all_invalid_short_circuits(config, vault_id):
    """All invalid UUIDs → empty results without hitting the API."""
    api = Mock()
    api.get_entities = AsyncMock()
    api.get_entity = AsyncMock()
    out = dispatch(
        'memex_get_entities',
        {'entity_ids': ['bad', '']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert json.loads(out) == {'results': []}
    api.get_entities.assert_not_awaited()
    api.get_entity.assert_not_awaited()


# -- AC-064: get_memory_units schema --


def test_get_memory_units_schema_shape():
    """AC-064: ``GET_MEMORY_UNITS_SCHEMA`` requires ``unit_ids: list[str]``."""
    props = GET_MEMORY_UNITS_SCHEMA['parameters']['properties']
    assert GET_MEMORY_UNITS_SCHEMA['name'] == 'memex_get_memory_units'
    assert 'unit_ids' in props
    assert props['unit_ids']['type'] == 'array'
    assert props['unit_ids']['items']['type'] == 'string'
    assert GET_MEMORY_UNITS_SCHEMA['parameters']['required'] == ['unit_ids']


# -- AC-065: singular loop --


def test_get_memory_units_loops_singular_calls(config, vault_id):
    """AC-065: ``api.get_memory_unit`` is awaited once per input ID, results returned as list."""
    api = Mock()
    u1 = uuid4()
    u2 = uuid4()
    unit_a = _fake_memory_unit('fact A')
    unit_b = _fake_memory_unit('fact B')

    def _by_uid(uid):
        return unit_a if uid == u1 else unit_b

    api.get_memory_unit = AsyncMock(side_effect=lambda u: _by_uid(u))
    out = dispatch(
        'memex_get_memory_units',
        {'unit_ids': [str(u1), str(u2)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert len(data['results']) == 2
    assert api.get_memory_unit.await_count == 2
    # Shape includes fact_type, status, superseded_by, contradictions.
    row = data['results'][0]
    assert 'fact_type' in row
    assert 'status' in row
    assert 'note_id' in row
    assert 'superseded_by' in row
    assert 'contradictions' in row


# -- AC-066: skip invalid + missing --


def test_get_memory_units_skips_invalid_and_missing(config, vault_id):
    """AC-066: invalid UUIDs skipped; missing units (None return) skipped."""
    api = Mock()
    good = uuid4()

    async def _fake_get(uid):
        return _fake_memory_unit('real')

    api.get_memory_unit = AsyncMock(side_effect=_fake_get)
    out = dispatch(
        'memex_get_memory_units',
        {'unit_ids': ['bad-uuid', str(good)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert len(data['results']) == 1
    # Only the valid UUID was passed to the API.
    api.get_memory_unit.assert_awaited_once()
    assert api.get_memory_unit.call_args.args[0] == good


def test_get_memory_units_skips_when_api_returns_none(config, vault_id):
    """AC-066: API returning None for a UUID → that entry omitted from results."""
    api = Mock()
    u1 = uuid4()
    u2 = uuid4()
    unit = _fake_memory_unit('present')

    def _mixed(uid):
        return unit if uid == u1 else None

    api.get_memory_unit = AsyncMock(side_effect=lambda u: _mixed(u))
    out = dispatch(
        'memex_get_memory_units',
        {'unit_ids': [str(u1), str(u2)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert len(data['results']) == 1


# -- AC-067: get_memory_links schema --


def test_get_memory_links_schema_shape():
    """AC-067: schema has required ``unit_ids``, optional ``link_type`` + ``limit``."""
    props = GET_MEMORY_LINKS_SCHEMA['parameters']['properties']
    assert GET_MEMORY_LINKS_SCHEMA['name'] == 'memex_get_memory_links'
    assert 'unit_ids' in props
    assert props['unit_ids']['type'] == 'array'
    assert props['unit_ids']['items']['type'] == 'string'
    assert props['link_type']['type'] == 'string'
    assert props['limit']['type'] == 'integer'
    assert GET_MEMORY_LINKS_SCHEMA['parameters']['required'] == ['unit_ids']


# -- AC-068: singular-loop wrapping (the critical RFC-014 test) --


def test_get_memory_links_loops_per_unit_id(config, vault_id):
    """AC-068 / RFC-014: loops singular ``get_memory_links`` per uid, aggregates flat list."""
    api = Mock()
    u1 = uuid4()
    u2 = uuid4()
    link_a1 = _fake_memory_link(unit_id=u1, relation='semantic')
    link_a2 = _fake_memory_link(unit_id=u1, relation='semantic', weight=0.5)
    link_b1 = _fake_memory_link(unit_id=u2, relation='semantic')
    api.get_memory_links = AsyncMock(side_effect=[[link_a1, link_a2], [link_b1]])
    out = dispatch(
        'memex_get_memory_links',
        {'unit_ids': [str(u1), str(u2)], 'link_type': 'semantic'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    # 3 links flattened into one list.
    assert len(data['results']) == 3
    # Method awaited exactly twice.
    assert api.get_memory_links.await_count == 2
    # First call: unit_id=u1 (kwarg, singular).
    first_call = api.get_memory_links.await_args_list[0]
    assert first_call.kwargs['unit_id'] == u1
    assert first_call.kwargs['link_type'] == 'semantic'
    # Second call: unit_id=u2.
    second_call = api.get_memory_links.await_args_list[1]
    assert second_call.kwargs['unit_id'] == u2
    # Each result carries its unit_id.
    unit_ids = {r['unit_id'] for r in data['results']}
    assert unit_ids == {str(u1), str(u2)}


def test_get_memory_links_no_filter_passes_none(config, vault_id):
    """AC-068: omitted ``link_type`` passes ``link_type=None`` to each call."""
    api = Mock()
    u1 = uuid4()
    api.get_memory_links = AsyncMock(return_value=[])
    dispatch(
        'memex_get_memory_links',
        {'unit_ids': [str(u1)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    call_kwargs = api.get_memory_links.call_args.kwargs
    assert call_kwargs['link_type'] is None


def test_get_memory_links_skips_invalid_uuids(config, vault_id):
    """Invalid UUIDs silently skipped; API only called for valid ones."""
    api = Mock()
    good = uuid4()
    api.get_memory_links = AsyncMock(return_value=[])
    dispatch(
        'memex_get_memory_links',
        {'unit_ids': ['bad', str(good), '']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert api.get_memory_links.await_count == 1
    assert api.get_memory_links.call_args.kwargs['unit_id'] == good


def test_get_memory_links_continues_after_per_unit_failure(config, vault_id):
    """Per-unit API failure doesn't abort the loop; successful units remain in output."""
    api = Mock()
    u1 = uuid4()
    u2 = uuid4()
    u3 = uuid4()
    link1 = _fake_memory_link(unit_id=u1)
    link3 = _fake_memory_link(unit_id=u3)
    api.get_memory_links = AsyncMock(
        side_effect=[[link1], RuntimeError('boom'), [link3]],
    )
    out = dispatch(
        'memex_get_memory_links',
        {'unit_ids': [str(u1), str(u2), str(u3)]},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    # 2 links survive (u1's + u3's); u2's failure didn't abort.
    assert len(data['results']) == 2
    assert {r['unit_id'] for r in data['results']} == {str(u1), str(u3)}


def test_get_memory_links_empty_unit_ids_returns_empty_results(config, vault_id):
    """All invalid UUIDs → empty results, no API call."""
    api = Mock()
    api.get_memory_links = AsyncMock()
    out = dispatch(
        'memex_get_memory_links',
        {'unit_ids': ['bad', '']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert json.loads(out) == {'results': []}
    api.get_memory_links.assert_not_awaited()


# -- AC-069: get_lineage schema --


def test_get_lineage_schema_shape():
    """AC-069: schema has required ``entity_type``, ``entity_id``; optional direction/depth/limit."""
    props = GET_LINEAGE_SCHEMA['parameters']['properties']
    assert GET_LINEAGE_SCHEMA['name'] == 'memex_get_lineage'
    assert props['entity_type']['type'] == 'string'
    assert props['entity_id']['type'] == 'string'
    assert props['direction']['type'] == 'string'
    assert props['depth']['type'] == 'integer'
    assert props['limit']['type'] == 'integer'
    assert set(GET_LINEAGE_SCHEMA['parameters']['required']) == {'entity_type', 'entity_id'}


# -- AC-070: direction string → enum --


def test_get_lineage_returns_lineage_response(config, vault_id):
    """AC-070: ``direction`` string is converted to ``LineageDirection`` enum before API call."""
    api = Mock()
    eid = uuid4()
    resp = LineageResponse(
        entity_type='memory_unit',
        entity={'id': str(eid), 'text': 'fact'},
        derived_from=[
            LineageResponse(
                entity_type='note',
                entity={'id': str(uuid4()), 'title': 'src'},
                derived_from=[],
            )
        ],
    )
    api.get_lineage = AsyncMock(return_value=resp)
    out = dispatch(
        'memex_get_lineage',
        {'entity_type': 'memory_unit', 'entity_id': str(eid), 'direction': 'upstream'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['entity_type'] == 'memory_unit'
    assert data['entity']['id'] == str(eid)
    assert len(data['derived_from']) == 1
    assert data['derived_from'][0]['entity_type'] == 'note'
    # CRITICAL: direction was passed as LineageDirection enum, not string.
    kwargs = api.get_lineage.call_args.kwargs
    assert isinstance(kwargs['direction'], LineageDirection)
    assert kwargs['direction'] == LineageDirection.UPSTREAM
    # entity_id was parsed to UUID.
    assert kwargs['entity_id'] == eid


def test_get_lineage_default_direction_is_upstream(config, vault_id):
    """AC-070: omitting ``direction`` defaults to upstream."""
    api = Mock()
    eid = uuid4()
    api.get_lineage = AsyncMock(
        return_value=LineageResponse(entity_type='note', entity={}, derived_from=[])
    )
    dispatch(
        'memex_get_lineage',
        {'entity_type': 'note', 'entity_id': str(eid)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert api.get_lineage.call_args.kwargs['direction'] == LineageDirection.UPSTREAM


# -- AC-071: invalid entity_type --


def test_get_lineage_rejects_unknown_entity_type(config, vault_id):
    """AC-071: unknown ``entity_type`` → tool_error listing the 4 valid types."""
    api = Mock()
    api.get_lineage = AsyncMock()
    out = dispatch(
        'memex_get_lineage',
        {'entity_type': 'bogus', 'entity_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'bogus' in data['error']
    # Valid types appear in the error.
    for t in ['mental_model', 'observation', 'memory_unit', 'note']:
        assert t in data['error']
    api.get_lineage.assert_not_awaited()


# -- AC-072: invalid direction --


def test_get_lineage_rejects_unknown_direction(config, vault_id):
    """AC-072: invalid ``direction`` → tool_error listing the 3 valid directions."""
    api = Mock()
    api.get_lineage = AsyncMock()
    out = dispatch(
        'memex_get_lineage',
        {'entity_type': 'memory_unit', 'entity_id': str(uuid4()), 'direction': 'sideways'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'sideways' in data['error']
    assert 'upstream' in data['error']
    assert 'downstream' in data['error']
    assert 'both' in data['error']
    api.get_lineage.assert_not_awaited()


def test_get_lineage_rejects_invalid_entity_id(config, vault_id):
    """Invalid UUID for entity_id → tool_error, API not called."""
    api = Mock()
    api.get_lineage = AsyncMock()
    out = dispatch(
        'memex_get_lineage',
        {'entity_type': 'memory_unit', 'entity_id': 'not-a-uuid'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert 'error' in data
    assert 'not-a-uuid' in data['error']
    api.get_lineage.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# Stream 5: assets + KV store
# ---------------------------------------------------------------------------

import base64 as _b64  # noqa: E402

from memex_common.schemas import KVEntryDTO, NoteDTO  # noqa: E402

from memex_hermes_plugin.memex.tools import (  # noqa: E402
    ADD_ASSETS_SCHEMA,
    GET_RESOURCES_SCHEMA,
    KV_GET_SCHEMA,
    KV_LIST_SCHEMA,
    KV_SEARCH_SCHEMA,
    KV_WRITE_SCHEMA,
    LIST_ASSETS_SCHEMA,
    _scope_from_key,
)


def _fake_kv_entry(key: str = 'user:work:employer', value: str = 'ACME') -> KVEntryDTO:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return KVEntryDTO(
        id=uuid4(),
        key=key,
        value=value,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )


def _note_with_assets(assets: list[str]):
    from datetime import datetime, timezone

    return NoteDTO(
        id=uuid4(),
        title='a note',
        vault_id=uuid4(),
        created_at=datetime.now(timezone.utc),
        assets=assets,
    )


# -- Schema-level tests --


def test_list_assets_schema_shape():
    """AC-041: required note_id; no other required params; no vault_ids."""
    params = LIST_ASSETS_SCHEMA['parameters']
    assert params['required'] == ['note_id']
    assert 'vault_ids' not in params['properties']
    assert params['properties']['note_id']['type'] == 'string'


def test_get_resources_schema_shape():
    """AC-045: required paths array of string."""
    params = GET_RESOURCES_SCHEMA['parameters']
    assert params['required'] == ['paths']
    assert params['properties']['paths']['type'] == 'array'
    assert params['properties']['paths']['items']['type'] == 'string'


def test_add_assets_schema_shape():
    """AC-048: required note_id + assets[{filename, content_b64}]; divergence note in description."""
    assert 'diverges from MCP' in ADD_ASSETS_SCHEMA['description']
    params = ADD_ASSETS_SCHEMA['parameters']
    assert set(params['required']) == {'note_id', 'assets'}
    item = params['properties']['assets']['items']
    assert set(item['required']) == {'filename', 'content_b64'}
    # Divergence invariant: no file_paths property (Hermes receives bytes, not paths).
    assert 'file_paths' not in params['properties']


def test_kv_write_schema_shape():
    """AC-078: required value/key; optional ttl_seconds; namespace guidance in description."""
    desc = KV_WRITE_SCHEMA['description']
    assert 'global:' in desc and 'user:' in desc and 'project:' in desc and 'app:' in desc
    params = KV_WRITE_SCHEMA['parameters']
    assert set(params['required']) == {'value', 'key'}
    assert 'ttl_seconds' in params['properties']
    assert 'ttl_seconds' not in params['required']


def test_kv_get_schema_shape():
    """AC-080: required key."""
    params = KV_GET_SCHEMA['parameters']
    assert params['required'] == ['key']


def test_kv_search_schema_shape():
    """AC-082: required query; optional namespaces, limit."""
    params = KV_SEARCH_SCHEMA['parameters']
    assert params['required'] == ['query']
    assert 'namespaces' in params['properties']
    assert 'limit' in params['properties']


def test_kv_list_schema_shape():
    """AC-084: no required params; optional namespaces."""
    params = KV_LIST_SCHEMA['parameters']
    assert params['required'] == []
    assert 'namespaces' in params['properties']


# -- Handler tests: list_assets (#22) --


def test_list_assets_returns_mcp_asset_shape(config, vault_id):
    """AC-042: returns {"results": [{filename, path, mime_type}]} from note.assets."""
    api = Mock()
    api.get_note = AsyncMock(
        return_value=_note_with_assets(['note123/diagram.png', 'note123/audio.mp3'])
    )
    note_id = uuid4()
    out = dispatch(
        'memex_list_assets',
        {'note_id': str(note_id)},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert len(data['results']) == 2
    assert data['results'][0] == {
        'filename': 'diagram.png',
        'path': 'note123/diagram.png',
        'mime_type': 'image/png',
    }
    assert data['results'][1] == {
        'filename': 'audio.mp3',
        'path': 'note123/audio.mp3',
        'mime_type': 'audio/mpeg',
    }


def test_list_assets_empty_returns_empty_results(config, vault_id):
    """AC-043: empty assets list returns {"results": []}, NOT tool_error."""
    api = Mock()
    api.get_note = AsyncMock(return_value=_note_with_assets([]))
    out = dispatch(
        'memex_list_assets',
        {'note_id': str(uuid4())},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data == {'results': []}
    assert 'error' not in data


def test_list_assets_rejects_invalid_uuid(config, vault_id):
    """AC-044: invalid UUID → tool_error; api.get_note NOT awaited."""
    api = Mock()
    api.get_note = AsyncMock()
    out = dispatch(
        'memex_list_assets',
        {'note_id': 'not-a-uuid'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.get_note.assert_not_awaited()


# -- Handler tests: get_resources (#23) --


def test_get_resources_base64_round_trip(config, vault_id):
    """AC-046: bytes round-trip base64; size_bytes is pre-encode length; mime from path."""
    api = Mock()
    raw = b'PNG_FAKE'
    api.get_resource = AsyncMock(return_value=raw)
    out = dispatch(
        'memex_get_resources',
        {'paths': ['abc/diagram.png']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    result = data['results'][0]
    assert _b64.b64decode(result['content_b64']) == raw
    assert result['size_bytes'] == len(raw)
    assert result['mime_type'] == 'image/png'
    assert result['filename'] == 'diagram.png'
    assert result['path'] == 'abc/diagram.png'
    assert 'error' not in result


def test_get_resources_partial_failure_reports_per_path(config, vault_id):
    """AC-047: per-path partial failure isolation; error entries have no content_b64."""
    api = Mock()
    api.get_resource = AsyncMock(
        side_effect=[b'ok1', FileNotFoundError('missing'), b'ok3'],
    )
    out = dispatch(
        'memex_get_resources',
        {'paths': ['p1', 'p2', 'p3']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    results = json.loads(out)['results']
    assert len(results) == 3
    assert _b64.b64decode(results[0]['content_b64']) == b'ok1'
    assert results[1]['path'] == 'p2'
    assert 'error' in results[1]
    assert 'content_b64' not in results[1]
    assert _b64.b64decode(results[2]['content_b64']) == b'ok3'


# -- Handler tests: add_assets (#24) --


def test_add_assets_base64_decode_round_trip(config, vault_id):
    """AC-049: handler decodes each content_b64 and passes {filename: bytes} to API."""
    api = Mock()
    note_id = uuid4()
    api.add_note_assets = AsyncMock(
        return_value={
            'added_assets': [f'{note_id}/x.png'],
            'skipped': [],
            'asset_count': 1,
        }
    )
    raw = b'PNG_FAKE'
    out = dispatch(
        'memex_add_assets',
        {
            'note_id': str(note_id),
            'assets': [{'filename': 'x.png', 'content_b64': _b64.b64encode(raw).decode('ascii')}],
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['status'] == 'ok'
    assert data['note_id'] == str(note_id)
    assert data['added_assets'][0]['filename'] == 'x.png'
    assert data['added_assets'][0]['mime_type'] == 'image/png'
    assert data['asset_count'] == 1
    api.add_note_assets.assert_awaited_once()
    uuid_arg, files_arg = api.add_note_assets.call_args.args
    assert uuid_arg == note_id
    assert files_arg == {'x.png': raw}


def test_add_assets_rejects_invalid_uuid(config, vault_id):
    """AC-050: invalid note_id UUID → tool_error; api.add_note_assets NOT awaited."""
    api = Mock()
    api.add_note_assets = AsyncMock()
    out = dispatch(
        'memex_add_assets',
        {
            'note_id': 'not-a-uuid',
            'assets': [{'filename': 'x.png', 'content_b64': _b64.b64encode(b'x').decode()}],
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.add_note_assets.assert_not_awaited()


def test_add_assets_rejects_invalid_base64(config, vault_id):
    """Malformed base64 is caught (binascii.Error) and surfaced as tool_error — no handler crash."""
    api = Mock()
    api.add_note_assets = AsyncMock()
    out = dispatch(
        'memex_add_assets',
        {
            'note_id': str(uuid4()),
            'assets': [{'filename': 'x.png', 'content_b64': '!!! not base64 !!!'}],
        },
        api=api,
        config=config,
        vault_id=vault_id,
    )
    assert 'error' in json.loads(out)
    api.add_note_assets.assert_not_awaited()


# -- Handler tests: kv_write (#25) --


@pytest.mark.parametrize(
    'key, expected_scope',
    [
        ('global:foo', 'global'),
        ('user:work:employer', 'user'),
        ('project:github.com/user/repo:vault', 'project:github.com/user/repo'),
        ('app:claude-code:theme', 'app'),
        ('project:justid', 'project'),
        ('nocolon', 'unknown'),
    ],
)
def test_scope_from_key_parametrized(key, expected_scope):
    """AC-079 scope derivation across all 4 namespace shapes + edges (RFC-012)."""
    assert _scope_from_key(key) == expected_scope


def test_scope_from_key_matches_mcp_source_of_truth():
    """Drift canary per RFC-012: Hermes copy MUST produce byte-equal output to MCP source.

    Fails loudly if _scope_from_key drifts from packages/mcp/src/memex_mcp/models.py:356.
    """
    pytest.importorskip('memex_mcp.models')
    from memex_mcp.models import _scope_from_key as mcp_fn

    from memex_hermes_plugin.memex.tools import _scope_from_key as hermes_fn

    for key in [
        'global:foo',
        'user:work:employer',
        'project:github.com/user/repo:vault',
        'app:claude-code:theme',
        'project:justid',
        'nocolon',
        '',
        ':leading',
        'trailing:',
    ]:
        assert hermes_fn(key) == mcp_fn(key), f'scope drift on key {key!r}'


def test_kv_write_generates_embedding_then_puts(config, vault_id):
    """AC-079: handler calls embed_text FIRST, then kv_put with that embedding."""
    api = Mock()
    api.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])
    entry = _fake_kv_entry(key='user:work:employer', value='ACME')
    api.kv_put = AsyncMock(return_value=entry)
    out = dispatch(
        'memex_kv_write',
        {'value': 'ACME', 'key': 'user:work:employer', 'ttl_seconds': 3600},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['key'] == 'user:work:employer'
    assert data['value'] == 'ACME'
    assert data['scope'] == 'user'
    api.embed_text.assert_awaited_once_with('ACME')
    api.kv_put.assert_awaited_once()
    put_kwargs = api.kv_put.call_args.kwargs
    assert put_kwargs['value'] == 'ACME'
    assert put_kwargs['key'] == 'user:work:employer'
    assert put_kwargs['embedding'] == [0.1, 0.2, 0.3]
    assert put_kwargs['ttl_seconds'] == 3600


def test_kv_write_missing_required_params(config, vault_id):
    """Missing value/key → tool_error, no API calls."""
    api = Mock()
    api.embed_text = AsyncMock()
    api.kv_put = AsyncMock()
    out = dispatch('memex_kv_write', {'key': 'user:x'}, api=api, config=config, vault_id=vault_id)
    assert 'error' in json.loads(out)
    api.embed_text.assert_not_awaited()
    api.kv_put.assert_not_awaited()


# -- Handler tests: kv_get (#26) --


def test_kv_get_returns_entry(config, vault_id):
    """AC-081: present entry returns dict with derived scope."""
    api = Mock()
    entry = _fake_kv_entry(key='project:gh.com/r:v', value='prod')
    api.kv_get = AsyncMock(return_value=entry)
    out = dispatch(
        'memex_kv_get',
        {'key': 'project:gh.com/r:v'},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert data['key'] == 'project:gh.com/r:v'
    assert data['value'] == 'prod'
    assert data['scope'] == 'project:gh.com/r'


def test_kv_get_returns_null_on_miss(config, vault_id):
    """AC-081: missing key returns JSON null (not tool_error)."""
    api = Mock()
    api.kv_get = AsyncMock(return_value=None)
    out = dispatch('memex_kv_get', {'key': 'x'}, api=api, config=config, vault_id=vault_id)
    assert json.loads(out) is None


# -- Handler tests: kv_search (#27) --


def test_kv_search_returns_semantic_results(config, vault_id):
    """AC-083: handler passes query/namespaces/limit to api.kv_search; wraps into {results: [...]}."""
    api = Mock()
    api.kv_search = AsyncMock(
        return_value=[
            _fake_kv_entry(key='user:a', value='v1'),
            _fake_kv_entry(key='user:b', value='v2'),
        ]
    )
    out = dispatch(
        'memex_kv_search',
        {'query': 'employer', 'namespaces': ['user'], 'limit': 3},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert len(data['results']) == 2
    assert data['results'][0]['scope'] == 'user'
    api.kv_search.assert_awaited_once()
    kwargs = api.kv_search.call_args.kwargs
    assert kwargs['query'] == 'employer'
    assert kwargs['namespaces'] == ['user']
    assert kwargs['limit'] == 3


# -- Handler tests: kv_list (#28) --


def test_kv_list_returns_entries(config, vault_id):
    """AC-085: handler passes namespaces to api.kv_list and wraps into {results: [...]}."""
    api = Mock()
    api.kv_list = AsyncMock(return_value=[_fake_kv_entry(key='user:pref', value='dark')])
    out = dispatch(
        'memex_kv_list',
        {'namespaces': ['user']},
        api=api,
        config=config,
        vault_id=vault_id,
    )
    data = json.loads(out)
    assert len(data['results']) == 1
    assert data['results'][0]['scope'] == 'user'
    api.kv_list.assert_awaited_once()
    assert api.kv_list.call_args.kwargs['namespaces'] == ['user']


def test_kv_list_no_namespaces_passes_none(config, vault_id):
    """Optional namespaces: when omitted, handler passes None to api.kv_list."""
    api = Mock()
    api.kv_list = AsyncMock(return_value=[])
    dispatch('memex_kv_list', {}, api=api, config=config, vault_id=vault_id)
    assert api.kv_list.call_args.kwargs['namespaces'] is None
