import pytest
import base64
from unittest.mock import patch
from fastapi.testclient import TestClient
from uuid import UUID
from sqlmodel import select

from memex_core.memory.sql_models import Note, Node, MemoryUnit, Chunk
from memex_core.memory.extraction.models import PageIndexOutput, TOCNode, PageIndexBlock


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e2e_page_index_strategy(client: TestClient, db_session):
    """
    Test the Page Index extraction strategy end-to-end.
    Verifies:
    1. Configuration enables 'page_index' strategy.
    2. Document ingestion triggers the Page Index pipeline.
    3. Hierarchical nodes (TOC) and blocks are persisted.
    4. Facts are extracted from blocks.
    """

    # 1. Setup Mock Page Index Output
    # We mock the internal 'index_document' call to avoid needing a real LLM for structure
    mock_toc = [
        TOCNode(
            id='node-1',
            title='Section 1',
            level=1,
            reasoning='Top level section',
            content='Content of section 1.',
            children=[],
            original_header_id=0,
        )
    ]
    mock_blocks = [
        PageIndexBlock(
            id='block-1',
            seq=0,
            token_count=100,
            start_index=0,
            end_index=50,
            titles_included=['Section 1'],
            content='Content of section 1.',
            node_id='node-1',
        )
    ]
    mock_output = PageIndexOutput(
        toc=mock_toc,
        blocks=mock_blocks,
        node_to_block_map={'node-1': 'block-1'},
        coverage_ratio=1.0,
        path_used='mock_path',
    )

    # Mock the fact extraction from the block content
    from memex_core.memory.extraction.models import RawFact
    from memex_core.memory.sql_models import TokenUsage

    mock_facts = [
        RawFact(
            what='Fact from section 1',
            fact_type='world',
            entities=[],
            chunk_index=0,
            content_index=0,
        )
    ]

    # We need to mock:
    # 1. index_document -> returns our mock structure
    # 2. extract_facts_from_chunks -> returns our mock facts
    # 3. embedding generation -> returns dummy vectors

    with (
        patch('memex_core.memory.extraction.engine.index_document') as mock_index_doc,
        patch('memex_core.memory.extraction.engine.extract_facts_from_chunks') as mock_extract,
        patch(
            'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'
        ) as mock_embed,
    ):
        mock_index_doc.return_value = (mock_output, TokenUsage())
        mock_extract.return_value = (mock_facts, [('Content of section 1.', 1)], TokenUsage())
        mock_embed.return_value = [[0.1] * 384] * 2  # One for block, one for fact

        # Force configuration to 'page_index'
        # The client fixture uses app.state.config, so we modify it there
        app = client.app
        original_strategy = app.state.api.config.server.memory.extraction.text_splitting.strategy
        app.state.api.config.server.memory.extraction.text_splitting.strategy = 'page_index'

        try:
            # 2. Ingest Document
            content = """# Section 1
Content of section 1."""
            b64_content = base64.b64encode(content.encode()).decode('utf-8')

            payload = {
                'name': 'Page Index Doc',
                'description': 'Testing Page Index Strategy',
                'content': b64_content,
                'tags': ['test'],
            }

            response = client.post('/api/v1/ingestions', json=payload)
            assert response.status_code == 200
            data = response.json()
            doc_id = data['note_id']

            # 3. Verify Database State

            # Check Document
            doc = await db_session.get(Note, UUID(doc_id))
            assert doc is not None
            assert doc.page_index is not None  # Should store the thin tree

            # Check Page Index Nodes
            stmt = select(Node).where(Node.note_id == UUID(doc_id))
            nodes = (await db_session.exec(stmt)).all()
            assert len(nodes) == 1
            assert nodes[0].title == 'Section 1'

            # Check Memory Blocks (Chunks)
            # In Page Index, blocks are stored as chunks
            stmt = select(Chunk).where(Chunk.note_id == UUID(doc_id))
            blocks = (await db_session.exec(stmt)).all()
            assert len(blocks) == 1
            assert blocks[0].text == 'Content of section 1.'

            # Check Extracted Facts (Memory Units)
            stmt = select(MemoryUnit).where(MemoryUnit.note_id == UUID(doc_id))
            units = (await db_session.exec(stmt)).all()
            assert len(units) == 1
            assert units[0].text == 'Fact from section 1'
        finally:
            # Restore configuration
            app.state.api.config.server.memory.extraction.text_splitting.strategy = (
                original_strategy
            )
