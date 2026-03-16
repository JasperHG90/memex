import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from datetime import datetime, timezone

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.extraction.engine import ExtractionEngine
from memex_core.config import ExtractionConfig
from memex_core.memory.extraction.models import (
    RetainContent,
    ExtractedFact,
    ChunkMetadata,
    FactTypes,
    ProcessedFact,
)
from memex_core.memory.sql_models import TokenUsage
from memex_core.memory.entity_resolver import EntityResolver


@pytest.fixture
def mock_session():
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def mock_lm():
    return MagicMock()


@pytest.fixture
def mock_predictor():
    mock = MagicMock()
    return mock


@pytest.fixture
def mock_embedding_model():
    mock = MagicMock()
    mock.encode.return_value = [[0.1] * 384]  # Single embedding
    return mock


@pytest.fixture
def mock_entity_resolver():
    mock = AsyncMock(spec=EntityResolver)
    mock.resolve_entities_batch.return_value = []
    mock.link_units_to_entities_batch.return_value = None
    return mock


@pytest.fixture
def extractor(mock_lm, mock_predictor, mock_embedding_model, mock_entity_resolver):
    config = ExtractionConfig()
    return ExtractionEngine(
        config,
        mock_lm,
        mock_predictor,
        mock_embedding_model,
        mock_entity_resolver,
    )

    @pytest.mark.asyncio
    async def test_extract_and_persist_empty(extractor, mock_session):
        ids, usage, touched = await extractor.extract_and_persist(mock_session, [])

        assert ids == []

        assert usage.total_tokens is None

        assert touched == set()


@pytest.mark.asyncio
async def test_extract_and_persist_flow(extractor, mock_session):
    # Mock _extract_facts result
    # We patch the private method or ensure the core dependency is mocked.
    # Ideally we mock the 'extract_facts_from_text' in 'memex_core.memory.extraction.core'
    # but here let's patch the extractor method itself for unit testing the orchestration.

    extracted_fact = ExtractedFact(
        fact_text='Test Fact',
        fact_type=FactTypes.WORLD,
        content_index=0,
        chunk_index=0,
        mentioned_at=datetime.now(timezone.utc),
    )
    chunk_meta = ChunkMetadata(chunk_text='Chunk', fact_count=1, content_index=0, chunk_index=0)

    # Patch internal methods to isolate orchestration logic
    extractor._extract_facts = AsyncMock(
        return_value=([extracted_fact], [chunk_meta], TokenUsage(total_tokens=10))
    )
    extractor._resolve_entities = AsyncMock(return_value={uuid4()})
    # Patch deduplication to avoid actual DB calls and force no-duplicate result
    with patch(
        'memex_core.memory.extraction.deduplication.check_duplicates_batch', new_callable=AsyncMock
    ) as mock_check_dup:
        mock_check_dup.return_value = [False]

        # Configure session mock for deduplication check (if still called by other logic)
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.exec.return_value = mock_result

        # Mock storage and pipeline functions
        with (
            patch(
                'memex_core.memory.extraction.storage.insert_facts_batch',
                new_callable=AsyncMock,
            ) as mock_insert,
            patch(
                'memex_core.memory.extraction.engine.track_document',
                new_callable=AsyncMock,
            ) as mock_track,
            patch(
                'memex_core.memory.extraction.engine.create_links',
                new_callable=AsyncMock,
            ) as mock_create_links,
            patch(
                'memex_core.memory.extraction.engine.process_embeddings',
                new_callable=AsyncMock,
            ) as mock_proc_emb,
            patch(
                'memex_core.memory.extraction.storage.store_chunks_batch',
                new_callable=AsyncMock,
            ) as mock_store_chunks,
        ):
            mock_insert.return_value = [str(uuid4())]
            mock_proc_emb.return_value = [
                ProcessedFact.from_extracted_fact(extracted_fact, [0.1] * 384)
            ]
            mock_store_chunks.return_value = {0: str(uuid4())}

            contents = [RetainContent(content='Test Content')]
            ids, usage, touched = await extractor.extract_and_persist(
                mock_session, contents, note_id=str(uuid4())
            )

            assert len(ids) == 1
            assert usage.total_tokens == 10
            assert len(touched) == 1
            extractor._extract_facts.assert_called_once()
            assert mock_proc_emb.call_count >= 1
            mock_track.assert_called_once()
            mock_store_chunks.assert_called_once()
            mock_insert.assert_called_once()
            extractor._resolve_entities.assert_called_once()
            mock_create_links.assert_called_once()


@pytest.mark.asyncio
async def test_persist_page_index_nodes_deduplicates_identical_content(extractor, mock_session):
    from memex_core.memory.extraction.models import (
        TOCNode,
        PageIndexOutput,
        content_hash_md5,
    )

    duplicate_content = 'Identical section text that appears twice.'
    node_a = TOCNode(
        reasoning='r',
        original_header_id=1,
        title='Section A',
        level=1,
        content=duplicate_content,
        token_estimate=20,
    )
    node_b = TOCNode(
        reasoning='r',
        original_header_id=2,
        title='Section B',
        level=1,
        content=duplicate_content,
        token_estimate=20,
    )

    pio = PageIndexOutput(toc=[node_a, node_b], blocks=[], node_to_block_map={})

    captured: list[dict] = []

    async def fake_insert(session, rows):
        captured.extend(rows)
        return [str(uuid4()) for _ in rows]

    with patch(
        'memex_core.memory.extraction.engine.storage.insert_nodes_batch',
        side_effect=fake_insert,
    ):
        await extractor._persist_page_index_nodes_and_blocks(
            session=mock_session,
            page_index_output=pio,
            note_id=str(uuid4()),
            vault_id=uuid4(),
        )

    assert len(captured) == 1
    assert captured[0]['title'] == 'Section A'  # first occurrence kept
    assert captured[0]['seq'] == 0
    assert captured[0]['node_hash'] == content_hash_md5(duplicate_content)


@pytest.mark.asyncio
async def test_persist_page_index_skips_duplicate_blocks(extractor, mock_session):
    """Blocks with duplicate id (content hash) should be deduped — only first stored."""
    from memex_core.memory.extraction.models import (
        TOCNode,
        PageIndexOutput,
        PageIndexBlock,
        content_hash_md5,
    )

    content = 'Repeated section content across parts.'
    block_hash = content_hash_md5(content)

    node_a = TOCNode(
        reasoning='r',
        original_header_id=1,
        title='Part I',
        level=1,
        content=content,
        token_estimate=20,
    )

    block_0 = PageIndexBlock(
        seq=0,
        content=content,
        id=block_hash,
        token_count=20,
        start_index=0,
        end_index=0,
        titles_included=['Part I'],
    )
    block_1 = PageIndexBlock(
        seq=1,
        content=content,
        id=block_hash,
        token_count=20,
        start_index=0,
        end_index=0,
        titles_included=['Part I'],
    )  # duplicate

    pio = PageIndexOutput(
        toc=[node_a],
        blocks=[block_0, block_1],
        node_to_block_map={},
    )

    captured_chunks: list[list[ChunkMetadata]] = []

    async def fake_store_chunks(session, note_id, chunks, vault_id=None):
        captured_chunks.append(chunks)
        return {c.chunk_index: str(uuid4()) for c in chunks}

    with (
        patch(
            'memex_core.memory.extraction.engine.storage.insert_nodes_batch',
            new_callable=AsyncMock,
            return_value=[str(uuid4())],
        ),
        patch(
            'memex_core.memory.extraction.engine.storage.store_chunks_batch',
            side_effect=fake_store_chunks,
        ),
        patch(
            'memex_core.memory.extraction.engine.embedding_processor.generate_embeddings_batch',
            new_callable=AsyncMock,
            return_value=[[0.1] * 384],
        ),
        patch(
            'memex_core.memory.extraction.engine.storage.backfill_node_block_ids',
            new_callable=AsyncMock,
        ),
    ):
        await extractor._persist_page_index_nodes_and_blocks(
            session=mock_session,
            page_index_output=pio,
            note_id=str(uuid4()),
            vault_id=uuid4(),
        )

    assert len(captured_chunks) == 1
    chunks = captured_chunks[0]
    assert len(chunks) == 1  # only first block stored
    assert chunks[0].chunk_index == 0
    assert chunks[0].content_hash == block_hash
