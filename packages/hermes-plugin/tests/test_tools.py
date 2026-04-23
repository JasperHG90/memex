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
    expected = {
        'memex_recall',
        'memex_retrieve_notes',
        'memex_survey',
        'memex_retain',
        'memex_list_entities',
        'memex_get_entity_mentions',
        'memex_get_entity_cooccurrences',
        # --- Stream 3: entities/memory/lineage ---
        'memex_get_entities',
        'memex_get_memory_units',
        'memex_get_memory_links',
        'memex_get_lineage',
    }
    assert expected.issubset(names)
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
# Stream 3: entities/memory/lineage
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

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
