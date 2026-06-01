"""E2E: the page-index → get_nodes → block_id → memory_units join (#194).

Passing a page-index node id to get_memory_units returned [] because the id
is an MD5 node_hash, not a chunk UUID. Exposing NodeDTO.block_id closes the
gap: get_nodes(page_index_id).block_id is the chunk UUID that
get_memory_units_by_chunks accepts.
"""

import base64
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from memex_core.memory.extraction.models import (
    PageIndexBlock,
    PageIndexOutput,
    RawFact,
    TOCNode,
)
from memex_core.memory.sql_models import Note


@pytest.mark.integration
@pytest.mark.asyncio
async def test_page_index_id_joins_to_memory_units(client: TestClient, db_session):
    section_content = '# Findings\nThe migration reduced p99 latency.'
    mock_toc = [
        TOCNode(
            id='node-1',
            title='Findings',
            level=1,
            reasoning='top',
            content=section_content,
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
            titles_included=['Findings'],
            content=section_content,
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
    mock_facts = [
        RawFact(
            what='The migration reduced p99 latency.',
            fact_type='world',
            entities=[],
            chunk_index=0,
            content_index=0,
        )
    ]

    with (
        patch('memex_core.memory.extraction.engine.index_document') as mock_index_doc,
        patch('memex_core.memory.extraction.engine.extract_facts_from_chunks') as mock_extract,
        patch(
            'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'
        ) as mock_embed,
        patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='Join Doc',
        ),
        patch(
            'memex_core.memory.contradiction.engine.ContradictionEngine.detect_contradictions',
            return_value=None,
        ),
    ):
        mock_index_doc.return_value = mock_output
        mock_extract.return_value = (mock_facts, [(section_content, 1)])
        mock_embed.side_effect = lambda _backend, texts: [[0.1] * 384 for _ in texts]

        app = client.app
        api = app.state.api
        original = api.config.server.memory.extraction.text_splitting.strategy
        api.config.server.memory.extraction.text_splitting.strategy = 'page_index'
        try:
            payload = {
                'name': 'Join Doc',
                'description': 'Chunk-id join test',
                'content': base64.b64encode(section_content.encode()).decode('utf-8'),
                'tags': ['test'],
            }
            resp = client.post('/api/v1/ingestions', json=payload)
            assert resp.status_code == 200
            doc_id = UUID(resp.json()['note_id'])

            note = await db_session.get(Note, doc_id)
            vault_id = str(note.vault_id)

            # Hop 1: page index returns MD5 hash ids in the TOC.
            pi_resp = client.get(f'/api/v1/notes/{doc_id}/page-index')
            assert pi_resp.status_code == 200
            toc_id_str = pi_resp.json()['page_index']['toc'][0]['id']  # MD5 hex

            # Hop 2: get_nodes resolves the hash id and exposes block_id.
            nodes_resp = client.post('/api/v1/nodes/batch', json={'node_ids': [toc_id_str]})
            assert nodes_resp.status_code == 200
            dtos = nodes_resp.json()
            assert len(dtos) == 1
            block_id = dtos[0]['block_id']
            assert block_id is not None, 'block_id must be populated for the chunk join'

            # Hop 3: the block_id is the chunk UUID memory units join on.
            units_resp = client.post(
                '/api/v1/memories/by-chunks',
                json={'chunk_ids': [block_id], 'vault_id': vault_id},
            )
            assert units_resp.status_code == 200
            assert units_resp.json(), 'page_index → block_id → memory_units must be non-empty'

            # The bug: the raw page-index id is NOT a chunk id.
            empty_resp = client.post(
                '/api/v1/memories/by-chunks',
                json={'chunk_ids': [toc_id_str], 'vault_id': vault_id},
            )
            assert empty_resp.status_code == 200
            assert empty_resp.json() == [], 'page-index id is not a chunk id (#194 trap)'
        finally:
            api.config.server.memory.extraction.text_splitting.strategy = original
