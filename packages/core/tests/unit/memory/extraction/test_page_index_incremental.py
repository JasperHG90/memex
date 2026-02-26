"""Unit tests for incremental PageIndex extraction with node-level change detection."""

import pytest
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.config import (
    ExtractionConfig,
    ConfidenceConfig,
    PageIndexTextSplitting,
    ModelConfig,
)
from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.memory.extraction.models import (
    RetainContent,
    TOCNode,
    PageIndexOutput,
    PageIndexBlock,
    content_hash_md5,
)
from memex_core.memory.sql_models import TokenUsage


# --- Fixtures ---


@pytest.fixture
def mock_session():
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def mock_lm():
    return MagicMock()


@pytest.fixture
def mock_predictor():
    return MagicMock()


@pytest.fixture
def mock_embedding_model():
    mock = MagicMock()
    mock.encode.return_value = [[0.1] * 384]
    return mock


@pytest.fixture
def mock_entity_resolver():
    mock = AsyncMock()
    mock.resolve_entities_batch.return_value = []
    mock.link_units_to_entities_batch.return_value = None
    return mock


@pytest.fixture
def page_index_config():
    return ExtractionConfig(
        model=ModelConfig(model='test/model'),
        text_splitting=PageIndexTextSplitting(
            model=ModelConfig(model='test/model'),
            scan_chunk_size_tokens=500,
            max_node_length_tokens=300,
            block_token_target=200,
            short_doc_threshold_tokens=100,
        ),
        active_strategy='page_index',
    )


@pytest.fixture
def extractor(
    page_index_config,
    mock_lm,
    mock_predictor,
    mock_embedding_model,
    mock_entity_resolver,
):
    return ExtractionEngine(
        config=page_index_config,
        confidence_config=ConfidenceConfig(),
        lm=mock_lm,
        predictor=mock_predictor,
        embedding_model=mock_embedding_model,
        entity_resolver=mock_entity_resolver,
        page_index_lm=mock_lm,
    )


def _make_toc_node(
    title: str,
    content: str,
    level: int = 1,
    children: list[TOCNode] | None = None,
    block_id: str | None = None,
) -> TOCNode:
    """Helper to build TOCNode with a deterministic content hash."""
    node = TOCNode(
        reasoning='test',
        original_header_id=1,
        title=title,
        level=level,
        content=content,
        token_estimate=len(content.split()),
        children=children or [],
        block_id=block_id,
    )
    # Assign content-hash ID
    node._assign_content_hash_ids()
    return node


def _make_block(seq: int, content: str, titles: list[str] | None = None) -> PageIndexBlock:
    return PageIndexBlock(
        id=content_hash_md5(content),
        seq=seq,
        token_count=len(content.split()),
        start_index=0,
        end_index=0,
        titles_included=titles or ['Section'],
        content=content,
    )


# --- Block diff classification ---


class TestBlockDiffClassification:
    """Test that block-level diff correctly classifies retained/new/removed."""

    def test_retained_blocks_identified(self):
        """Blocks with matching hashes are classified as retained."""
        existing_hash = content_hash_md5('unchanged content')
        existing_blocks = [{'id': uuid4(), 'content_hash': existing_hash, 'chunk_index': 0}]
        new_blocks = [_make_block(0, 'unchanged content')]

        existing_hash_set = {str(b['content_hash']) for b in existing_blocks}
        new_hash_set = {b.id for b in new_blocks}

        retained = new_hash_set & existing_hash_set
        assert len(retained) == 1
        assert existing_hash in retained

    def test_new_blocks_identified(self):
        """Blocks with hashes not in existing set are classified as new."""
        existing_blocks = [
            {'id': uuid4(), 'content_hash': content_hash_md5('old'), 'chunk_index': 0}
        ]
        new_blocks = [_make_block(0, 'new content')]

        existing_hash_set = {str(b['content_hash']) for b in existing_blocks}
        new_hash_set = {b.id for b in new_blocks}

        new_hashes = new_hash_set - existing_hash_set
        assert len(new_hashes) == 1

    def test_removed_blocks_identified(self):
        """Blocks in existing but not in new set are classified as removed."""
        old_hash = content_hash_md5('deleted content')
        existing_blocks = [
            {'id': uuid4(), 'content_hash': old_hash, 'chunk_index': 0},
            {'id': uuid4(), 'content_hash': content_hash_md5('kept'), 'chunk_index': 1},
        ]
        new_blocks = [_make_block(0, 'kept')]

        existing_hash_set = {str(b['content_hash']) for b in existing_blocks}
        new_hash_set = {b.id for b in new_blocks}

        removed = existing_hash_set - new_hash_set
        assert old_hash in removed
        assert len(removed) == 1


# --- Node-level change detection ---


class TestNodeLevelDetection:
    """Test node-level classification into BOUNDARY_SHIFT vs CONTENT_CHANGED."""

    def test_all_existing_nodes_means_boundary_shift(self):
        """A new block whose nodes all existed before is BOUNDARY_SHIFT."""
        node_a = _make_toc_node('A', 'content of section A')
        node_b = _make_toc_node('B', 'content of section B')

        # Previous node hashes include both
        prev_node_hash_set = {node_a.content_hash, node_b.content_hash}

        # New block combines them with a different block hash
        merged_content = 'content of section A\ncontent of section B'
        block = _make_block(0, merged_content)

        # Simulate node_to_block_map
        block_node_hashes: dict[str, set[str]] = defaultdict(set)
        block_node_hashes[block.id].add(node_a.content_hash)
        block_node_hashes[block.id].add(node_b.content_hash)

        node_hashes = block_node_hashes.get(block.id, set())
        assert node_hashes.issubset(prev_node_hash_set)

    def test_new_node_in_block_means_content_changed(self):
        """A new block containing a node not seen before is CONTENT_CHANGED."""
        node_existing = _make_toc_node('A', 'existing content')
        node_new = _make_toc_node('B', 'completely new content')

        prev_node_hash_set = {node_existing.content_hash}

        block = _make_block(0, 'merged content')
        block_node_hashes: dict[str, set[str]] = defaultdict(set)
        block_node_hashes[block.id].add(node_existing.content_hash)
        block_node_hashes[block.id].add(node_new.content_hash)

        node_hashes = block_node_hashes.get(block.id, set())
        assert not node_hashes.issubset(prev_node_hash_set)

    def test_empty_node_hashes_means_content_changed(self):
        """A block with no node hash info defaults to CONTENT_CHANGED."""
        block = _make_block(0, 'some content')
        block_node_hashes: dict[str, set[str]] = defaultdict(set)
        prev_node_hash_set: set[str] = {'some_old_hash'}

        node_hashes = block_node_hashes.get(block.id, set())
        # Empty set is not a subset in the "all nodes existed" sense
        assert not (node_hashes and node_hashes.issubset(prev_node_hash_set))


# --- Fact migration mapping ---


class TestFactMigrationMapping:
    """Test that fact migration picks the new chunk with the most node overlap."""

    def test_migration_picks_best_overlap(self):
        """Old chunk's facts should migrate to the new chunk sharing the most nodes."""
        # Old chunk had nodes A, B, C
        old_chunk_id = uuid4()
        old_node_map = {old_chunk_id: {'hash_a', 'hash_b', 'hash_c'}}

        # New boundary-shift blocks
        new_block_1_nodes = {'hash_a', 'hash_b'}  # 2 overlap
        new_block_2_nodes = {'hash_c', 'hash_d'}  # 1 overlap

        new_block_1_chunk_id = uuid4()
        new_block_2_chunk_id = uuid4()

        candidates = [
            (new_block_1_nodes, new_block_1_chunk_id),
            (new_block_2_nodes, new_block_2_chunk_id),
        ]

        old_nodes = old_node_map[old_chunk_id]
        best_new_chunk_id = None
        best_overlap = 0
        for nodes, chunk_id in candidates:
            overlap = len(old_nodes & nodes)
            if overlap > best_overlap:
                best_overlap = overlap
                best_new_chunk_id = chunk_id

        assert best_new_chunk_id == new_block_1_chunk_id
        assert best_overlap == 2


# --- Short-circuit behavior ---


class TestShortCircuit:
    @pytest.mark.asyncio
    async def test_no_changes_skips_hindsight(self, extractor, mock_session):
        """When all blocks are retained, no fact extraction should occur."""
        doc_id = str(uuid4())
        vault_id = uuid4()
        content_text = 'unchanged document content'
        block_hash = content_hash_md5(content_text)

        existing_blocks = [{'id': uuid4(), 'content_hash': block_hash, 'chunk_index': 0}]

        node = _make_toc_node('Title', content_text, block_id=block_hash)
        block = _make_block(0, content_text)
        pio = PageIndexOutput(
            toc=[node],
            blocks=[block],
            node_to_block_map={node.id: block.id},
            path_used='test',
            coverage_ratio=1.0,
        )

        contents = [RetainContent(content=content_text, vault_id=vault_id)]

        with (
            patch(
                'memex_core.memory.extraction.engine.index_document',
                new_callable=AsyncMock,
                return_value=(pio, TokenUsage()),
            ),
            patch(
                'memex_core.memory.extraction.engine.storage.get_note_nodes',
                new_callable=AsyncMock,
                return_value=[
                    {
                        'id': uuid4(),
                        'node_hash': node.content_hash,
                        'block_id': existing_blocks[0]['id'],
                        'seq': 0,
                    }
                ],
            ),
            patch(
                'memex_core.memory.extraction.engine.storage.get_node_hashes_by_block',
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                'memex_core.memory.extraction.engine.storage.reindex_blocks',
                new_callable=AsyncMock,
            ) as mock_reindex,
            patch(
                'memex_core.memory.extraction.engine.storage.mark_blocks_stale',
                new_callable=AsyncMock,
            ),
            patch(
                'memex_core.memory.extraction.engine.storage.mark_memory_units_stale',
                new_callable=AsyncMock,
            ),
            patch(
                'memex_core.memory.extraction.engine.storage.mark_nodes_stale',
                new_callable=AsyncMock,
            ),
            patch(
                'memex_core.memory.extraction.engine.storage.migrate_facts_to_chunks',
                new_callable=AsyncMock,
            ),
            patch(
                'memex_core.memory.extraction.engine.storage.update_note_page_index',
                new_callable=AsyncMock,
            ),
            patch(
                'memex_core.memory.extraction.engine.storage.update_note_title',
                new_callable=AsyncMock,
            ),
            patch(
                'memex_core.memory.extraction.engine.resolve_title_from_page_index',
                new_callable=AsyncMock,
                return_value='Title',
            ),
            patch(
                'memex_core.memory.extraction.engine.extract_facts_from_chunks',
                new_callable=AsyncMock,
            ) as mock_extract_facts,
            patch.object(
                extractor, '_persist_page_index_nodes_and_blocks', new_callable=AsyncMock
            ) as mock_persist,
            patch.object(extractor, '_track_document', new_callable=AsyncMock),
        ):
            mock_persist.return_value = ([], {})

            unit_ids, usage, touched = await extractor._extract_page_index_incremental(
                session=mock_session,
                contents=contents,
                agent_name='test',
                note_id=doc_id,
                existing_blocks=existing_blocks,
                vault_id=vault_id,
            )

            assert unit_ids == []
            assert touched == set()
            mock_extract_facts.assert_not_called()
            mock_reindex.assert_called_once()


# --- Routing in extract_and_persist ---


class TestRouting:
    @pytest.mark.asyncio
    async def test_page_index_strategy_routes_to_incremental(self, extractor, mock_session):
        """extract_and_persist routes to _extract_page_index_incremental for page_index strategy."""
        doc_id = str(uuid4())
        vault_id = uuid4()
        contents = [RetainContent(content='some text', vault_id=vault_id)]

        existing_blocks = [{'id': uuid4(), 'content_hash': 'hash1', 'chunk_index': 0}]

        with (
            patch(
                'memex_core.memory.extraction.engine.storage.get_note_blocks',
                new_callable=AsyncMock,
                return_value=existing_blocks,
            ),
            patch.object(
                extractor,
                '_extract_page_index_incremental',
                new_callable=AsyncMock,
                return_value=([], TokenUsage(), set()),
            ) as mock_incremental,
        ):
            await extractor.extract_and_persist(
                session=mock_session,
                contents=contents,
                note_id=doc_id,
                is_first_batch=True,
            )

            mock_incremental.assert_called_once()

    @pytest.mark.asyncio
    async def test_simple_strategy_routes_to_extract_incremental(
        self, mock_session, mock_lm, mock_predictor, mock_embedding_model, mock_entity_resolver
    ):
        """extract_and_persist routes to _extract_incremental for simple strategy."""
        config = ExtractionConfig()
        engine = ExtractionEngine(
            config=config,
            confidence_config=ConfidenceConfig(),
            lm=mock_lm,
            predictor=mock_predictor,
            embedding_model=mock_embedding_model,
            entity_resolver=mock_entity_resolver,
        )

        doc_id = str(uuid4())
        vault_id = uuid4()
        contents = [RetainContent(content='some text', vault_id=vault_id)]
        existing_blocks = [{'id': uuid4(), 'content_hash': 'hash1', 'chunk_index': 0}]

        with (
            patch(
                'memex_core.memory.extraction.engine.storage.get_note_blocks',
                new_callable=AsyncMock,
                return_value=existing_blocks,
            ),
            patch.object(
                engine,
                '_extract_incremental',
                new_callable=AsyncMock,
                return_value=([], TokenUsage(), set()),
            ) as mock_simple_incr,
        ):
            await engine.extract_and_persist(
                session=mock_session,
                contents=contents,
                note_id=doc_id,
                is_first_batch=True,
            )

            mock_simple_incr.assert_called_once()
