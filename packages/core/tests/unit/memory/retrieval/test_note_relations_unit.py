"""Unit tests for note & memory unit relationship DTOs, models, and integration points."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_common.schemas import (
    MemoryLinkDTO,
    NoteSearchResult,
    RelatedNoteDTO,
)
from memex_mcp.models import (
    McpFact,
    McpMemoryLink,
    McpNoteSearchResult,
    McpPageIndex,
    McpPageMetadata,
    McpRelatedNote,
)


# ---------------------------------------------------------------------------
# 1-3. DTO construction and serialization
# ---------------------------------------------------------------------------


class TestMemoryLinkDTO:
    def test_construct_and_serialize(self):
        uid = uuid4()
        nid = uuid4()
        now = dt.datetime.now(dt.timezone.utc)
        dto = MemoryLinkDTO(
            unit_id=uid,
            note_id=nid,
            note_title='Test Note',
            relation='contradicts',
            weight=0.85,
            time=now,
            metadata={'reason': 'newer data'},
        )
        data = dto.model_dump(mode='json')
        assert data['unit_id'] == str(uid)
        assert data['note_id'] == str(nid)
        assert data['note_title'] == 'Test Note'
        assert data['relation'] == 'contradicts'
        assert data['weight'] == 0.85
        assert data['metadata'] == {'reason': 'newer data'}

    def test_defaults(self):
        dto = MemoryLinkDTO(unit_id=uuid4(), relation='temporal')
        assert dto.note_id is None
        assert dto.note_title is None
        assert dto.weight == 1.0
        assert dto.time is None
        assert dto.metadata == {}


class TestRelatedNoteDTO:
    def test_construct_and_serialize(self):
        nid = uuid4()
        dto = RelatedNoteDTO(
            note_id=nid,
            title='Related Note',
            shared_entities=['Python', 'FastAPI'],
            strength=0.92,
        )
        data = dto.model_dump(mode='json')
        assert data['note_id'] == str(nid)
        assert data['title'] == 'Related Note'
        assert data['shared_entities'] == ['Python', 'FastAPI']
        assert data['strength'] == 0.92

    def test_defaults(self):
        dto = RelatedNoteDTO(note_id=uuid4())
        assert dto.title is None
        assert dto.shared_entities == []
        assert dto.strength == 0.0


class TestNoteSearchResultFields:
    def test_new_fields_default_to_empty_lists(self):
        result = NoteSearchResult(note_id=uuid4(), metadata={'title': 'Test'})
        assert result.related_notes == []
        assert result.links == []

    def test_new_fields_populated(self):
        rn = RelatedNoteDTO(note_id=uuid4(), title='Related', strength=0.5)
        lnk = MemoryLinkDTO(unit_id=uuid4(), relation='semantic')
        result = NoteSearchResult(
            note_id=uuid4(),
            metadata={'title': 'Test'},
            related_notes=[rn],
            links=[lnk],
        )
        assert len(result.related_notes) == 1
        assert len(result.links) == 1

    def test_backward_compatible(self):
        """Constructing without new fields works — backward compatible."""
        result = NoteSearchResult(note_id=uuid4(), metadata={}, score=1.5)
        data = result.model_dump(mode='json')
        assert data['related_notes'] == []
        assert data['links'] == []


# ---------------------------------------------------------------------------
# 4-8. MCP model tests
# ---------------------------------------------------------------------------


class TestMcpMemoryLink:
    def test_construct(self):
        uid = uuid4()
        link = McpMemoryLink(
            unit_id=uid,
            note_id=uuid4(),
            note_title='Note',
            relation='temporal',
            weight=0.7,
            time='2026-01-01T00:00:00+00:00',
            metadata={'key': 'val'},
        )
        assert isinstance(link.time, str)
        assert link.relation == 'temporal'

    def test_defaults(self):
        link = McpMemoryLink(unit_id=uuid4(), relation='semantic')
        assert link.time is None
        assert link.metadata == {}


class TestMcpRelatedNote:
    def test_construct(self):
        rn = McpRelatedNote(
            note_id=uuid4(),
            title='Test',
            shared_entities=['A', 'B'],
            strength=0.8,
        )
        assert rn.shared_entities == ['A', 'B']
        assert rn.strength == 0.8


class TestMcpMemoryUnitBaseLinks:
    def test_links_cascade_to_mcpfact(self):
        link = McpMemoryLink(unit_id=uuid4(), relation='temporal')
        fact = McpFact(
            id=uuid4(),
            text='A fact',
            links=[link],
        )
        assert len(fact.links) == 1
        assert fact.links[0].relation == 'temporal'

    def test_links_default_empty(self):
        fact = McpFact(id=uuid4(), text='A fact')
        assert fact.links == []


class TestMcpNoteSearchResultFields:
    def test_new_fields(self):
        rn = McpRelatedNote(note_id=uuid4(), title='R', strength=0.5)
        lnk = McpMemoryLink(unit_id=uuid4(), relation='semantic')
        result = McpNoteSearchResult(
            note_id=uuid4(),
            title='Test',
            score=1.0,
            related_notes=[rn],
            links=[lnk],
        )
        assert len(result.related_notes) == 1
        assert len(result.links) == 1

    def test_defaults(self):
        result = McpNoteSearchResult(note_id=uuid4(), title='T', score=0.5)
        assert result.related_notes == []
        assert result.links == []


class TestMcpPageIndexRelatedNotes:
    def test_related_notes_field(self):
        rn = McpRelatedNote(note_id=uuid4(), title='R', strength=0.9)
        pi = McpPageIndex(
            note_id=uuid4(),
            metadata=McpPageMetadata(title='Test'),
            toc=[],
            related_notes=[rn],
        )
        assert len(pi.related_notes) == 1
        data = pi.model_dump(mode='json')
        assert len(data['related_notes']) == 1

    def test_defaults(self):
        pi = McpPageIndex(
            note_id=uuid4(),
            metadata=McpPageMetadata(title='Test'),
            toc=[],
        )
        assert pi.related_notes == []


# ---------------------------------------------------------------------------
# 9. _hydrate_results link attachment (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hydrate_results_attaches_links():
    """Verify _hydrate_results calls fetch_memory_links and attaches to unit_metadata."""
    from memex_core.memory.retrieval.engine import RetrievalEngine
    from memex_core.memory.sql_models import MemoryUnit

    uid = uuid4()
    nid = uuid4()
    link_dto = MemoryLinkDTO(unit_id=uuid4(), note_id=nid, relation='semantic', weight=0.9)

    unit = MemoryUnit(
        id=uid,
        text='Test fact',
        fact_type='world',
        vault_id=uuid4(),
        embedding=[0.1] * 384,
    )
    unit.unit_metadata = {}

    # Mock session.exec to return our unit for unit queries and empty for others
    mock_result = MagicMock()
    mock_result.all.return_value = [unit]
    mock_session = AsyncMock()
    mock_session.exec.return_value = mock_result

    engine = RetrievalEngine(embedder=MagicMock(), reranker=MagicMock())

    ranked_item = MagicMock()
    ranked_item.id = uid
    ranked_item.type = 'unit'

    with patch(
        'memex_core.memory.retrieval.note_relations.fetch_memory_links',
        new_callable=AsyncMock,
        return_value={uid: [link_dto]},
    ):
        results = await engine._hydrate_results(mock_session, [ranked_item])

    assert len(results) == 1
    assert 'links' in results[0].unit_metadata
    assert len(results[0].unit_metadata['links']) == 1
    assert results[0].unit_metadata['links'][0]['relation'] == 'semantic'


# ---------------------------------------------------------------------------
# 10. _build_memory_unit_model link propagation
# ---------------------------------------------------------------------------


def test_build_memory_unit_model_extracts_links():
    """Verify _build_memory_unit_model extracts links from unit_metadata."""
    from memex_mcp.server import _build_memory_unit_model

    uid = uuid4()
    link_data = {
        'unit_id': str(uuid4()),
        'relation': 'temporal',
        'weight': 0.8,
    }

    mock_unit = MagicMock()
    mock_unit.id = uid
    mock_unit.text = 'Test fact'
    mock_unit.fact_type = 'world'
    mock_unit.score = 0.9
    mock_unit.confidence = 1.0
    mock_unit.note_id = None
    mock_unit.node_ids = []
    mock_unit.status = 'active'
    mock_unit.metadata = {'tags': [], 'links': [link_data]}
    mock_unit.superseded_by = []
    mock_unit.occurred_start = None
    mock_unit.occurred_end = None
    mock_unit.mentioned_at = None

    result = _build_memory_unit_model(mock_unit)

    assert len(result.links) == 1
    assert result.links[0].relation == 'temporal'
    assert result.links[0].weight == 0.8


def test_build_memory_unit_model_no_links():
    """When no links in metadata, output has empty links list."""
    from memex_mcp.server import _build_memory_unit_model

    mock_unit = MagicMock()
    mock_unit.id = uuid4()
    mock_unit.text = 'Test fact'
    mock_unit.fact_type = 'world'
    mock_unit.score = 0.9
    mock_unit.confidence = 1.0
    mock_unit.note_id = None
    mock_unit.node_ids = []
    mock_unit.status = 'active'
    mock_unit.metadata = {'tags': []}
    mock_unit.superseded_by = []

    result = _build_memory_unit_model(mock_unit)
    assert result.links == []


# ---------------------------------------------------------------------------
# 13. RemoteMemexAPI.get_related_notes mock test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_memex_api_get_related_notes():
    """Verify RemoteMemexAPI.get_related_notes deserializes response correctly."""
    from memex_common.client import RemoteMemexAPI

    nid = uuid4()
    rn_id = uuid4()
    mock_response = {
        str(nid): [
            {
                'note_id': str(rn_id),
                'title': 'Related',
                'shared_entities': ['Python'],
                'strength': 0.85,
            }
        ]
    }

    api = RemoteMemexAPI.__new__(RemoteMemexAPI)
    with patch.object(api, '_post', new_callable=AsyncMock, return_value=mock_response):
        result = await api.get_related_notes([nid])

    assert nid in result
    assert len(result[nid]) == 1
    assert result[nid][0].note_id == rn_id
    assert result[nid][0].title == 'Related'
    assert result[nid][0].shared_entities == ['Python']
    assert result[nid][0].strength == 0.85


# ---------------------------------------------------------------------------
# Static: _boost_linked_notes removal verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_search_enriches_results_with_relations():
    """Verify NoteSearchEngine.search attaches related_notes and links to results."""
    from memex_core.memory.retrieval.document_search import NoteSearchEngine, NoteSearchRequest
    from memex_common.schemas import NoteSearchResult

    nid_a = uuid4()
    nid_b = uuid4()
    uid = uuid4()

    result_a = NoteSearchResult(note_id=nid_a, metadata={'title': 'Note A'}, score=0.9)

    related_dto = RelatedNoteDTO(
        note_id=nid_b, title='Note B', shared_entities=['Python'], strength=0.8
    )
    link_dto = MemoryLinkDTO(
        unit_id=uid, note_id=nid_b, note_title='Note B', relation='semantic', weight=0.7
    )

    # Mock embedder to return a numpy-like array
    mock_embedder = MagicMock()
    mock_embed_result = MagicMock()
    mock_embed_result.tolist.return_value = [0.1] * 384
    mock_embedder.encode.return_value = [mock_embed_result]

    engine = NoteSearchEngine(embedder=mock_embedder, ner_model=None, lm=None)

    mock_session = AsyncMock()

    mock_chunk = MagicMock()
    mock_chunk.note_id = nid_a
    mock_chunk.text = 'chunk text'
    fake_chunks = [mock_chunk]

    with (
        patch.object(
            engine,
            '_search_single_query',
            new_callable=AsyncMock,
            return_value=fake_chunks,
        ),
        patch.object(engine, '_fuse_multi_query', return_value=[(mock_chunk, 0.9)]),
        patch.object(
            engine,
            '_group_by_document',
            new_callable=AsyncMock,
            return_value=[result_a],
        ),
        patch(
            'memex_core.memory.retrieval.note_relations.compute_related_notes',
            new_callable=AsyncMock,
            return_value={nid_a: [related_dto]},
        ),
        patch(
            'memex_core.memory.retrieval.note_relations.fetch_memory_links_for_notes',
            new_callable=AsyncMock,
            return_value={nid_a: [link_dto]},
        ),
        patch('asyncio.to_thread', new_callable=AsyncMock, return_value=[mock_embed_result]),
    ):
        request = NoteSearchRequest(query='test', vault_ids=[uuid4()])
        results = await engine.search(mock_session, request)

    assert len(results) == 1
    assert len(results[0].related_notes) == 1
    assert results[0].related_notes[0].note_id == nid_b
    assert results[0].related_notes[0].shared_entities == ['Python']
    assert len(results[0].links) == 1
    assert results[0].links[0].relation == 'semantic'
    assert results[0].links[0].weight == 0.7


def test_boost_linked_notes_removed():
    """Verify _boost_linked_notes, LINKED_NOTE_BOOST, MIN_TITLE_LENGTH are removed."""
    import memex_core.memory.retrieval.document_search as ds

    assert not hasattr(ds, 'LINKED_NOTE_BOOST')
    assert not hasattr(ds, 'MIN_TITLE_LENGTH')
    assert not hasattr(ds.NoteSearchEngine, '_boost_linked_notes')
